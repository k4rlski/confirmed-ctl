"""SQLAlchemy models for confirmed-ctl.

Tables owned by confirmed-ctl (all in the standalone confirmed_ctl Postgres):
``bank_transactions``, ``ad_confirmations``, ``confirmed_ctl_sync_log``
(see docs/ARCHITECTURE-SPEC.md Section 3).

Cross-DB rule (locked data architecture): ad / case data lives ONLY in the
MariaDB CRM ``permtrak2_crm.t_e_s_t_p_e_r_m`` (read-only) and is NEVER a Postgres
table here. Therefore the standalone Postgres has NO ``ad_purchases`` table and
NO foreign key may point at one. Confirmed-ctl references a CRM ad *logically*
via plain, indexed columns (``ad_crm_id`` = the EspoCRM record id, ``ad_number``
= the CRM ``adnumbernews`` value) with NO ``ForeignKey`` constraint. The live
read-only lookup adapter into ``t_e_s_t_p_e_r_m`` lands in a later generation;
until then a CRM ad is represented in-process by the lightweight ``CrmAd``
read model below (not an ORM table).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# No tables from other databases are owned/migrated here. Kept (empty) so the
# alembic env.py autogenerate filter has a stable symbol to import; ad/case data
# lives in the MariaDB CRM and is never modelled as a Postgres table.
EXTERNAL_TABLES: frozenset[str] = frozenset()

# Allowed values for ``bank_transactions.source`` — one per ingestion adapter.
# Adapters MUST use exactly these strings (enforced by a DB CHECK constraint).
BANK_TXN_SOURCES = ("email-scan", "export-ofx", "export-csv")


class Base(DeclarativeBase):
    pass


@dataclass
class CrmAd:
    """Read-only, in-process view of a newspaper-ad record from the MariaDB CRM
    (``permtrak2_crm.t_e_s_t_p_e_r_m``).

    This is deliberately NOT a SQLAlchemy model / Postgres table — ad data is
    never persisted here (see module docstring). The scorer and candidate lookups
    accept one of these. The adapter that hydrates it from the live CRM (via a
    read-only MariaDB query) lands in a later generation; for now callers may
    construct it directly (e.g. tests) or it is produced by that future adapter.

    ``crm_id`` is the EspoCRM record id and ``ad_number`` is the CRM
    ``adnumbernews`` value (e.g. ``"12446969"`` / ``"IPR00160880"``).
    """

    crm_id: str | None = None
    ad_number: str | None = None
    client_name: str | None = None
    newspaper_name: str | None = None
    run_date: date | None = None
    expected_charge_date: date | None = None
    expected_amount: float | None = None
    # Richer ad-identifying fields surfaced for the MARS reconcile page (ABCF-X
    # style columns). All read-only from the CRM ``t_e_s_t_p_e_r_m`` row.
    case_number: str | None = None
    state: str | None = None
    attorney: str | None = None
    entity: str | None = None
    # Additional ABCF-X reconcile columns. ``run_end`` is the ad's news end date
    # (``datenewsend``); ``run_date`` above is the start (``datenewsstart``).
    # ``status_news`` is the raw EspoCRM ``statnews`` enum string (e.g.
    # ``'["Active"]'``) passed through as-is. ``owner`` is ``news.owner``.
    job_title: str | None = None
    run_end: date | None = None
    status_news: str | None = None
    owner: str | None = None
    # Additional ABCF-X contract columns surfaced for the reconcile page.
    # ``approved_date`` is the ad approval date (``adsapproveddate``);
    # ``buy_date`` is ``datebuynews`` exposed distinctly from
    # ``expected_charge_date`` (which falls back to the run start when buy is
    # NULL). ``beneficiary_last`` is ``beneficiarylast``. ``clearance_status`` is
    # the raw EspoCRM ``statclearancenews`` enum string (e.g. ``'["Confirmed"]'``)
    # passed through as-is.
    approved_date: date | None = None
    buy_date: date | None = None
    beneficiary_last: str | None = None
    clearance_status: str | None = None


class BankTransaction(Base):
    """A bank transaction ingested from a source (BofA email-scan / export).

    ``source`` names the ingestion adapter that produced the row and
    ``source_txn_id`` is that source's own stable identifier for the
    transaction. Both are ``NOT NULL`` and the pair is UNIQUE, so re-ingesting
    the same transaction is idempotent (the composite unique constraint
    ``uq_bank_transactions_source_txn`` collapses duplicates).

    ``source`` is restricted by a CHECK constraint to the ingestion adapters:
    ``email-scan`` / ``export-ofx`` / ``export-csv`` (see ``BANK_TXN_SOURCES``).

    ``source_txn_id`` is ALWAYS populated (never ``NULL``). The convention,
    implemented by ``confirmed_ctl.ingest.dedup.deterministic_source_txn_id``:

    - **OFX** (``export-ofx``) → the statement ``<FITID>``.
    - **email-scan** (``email-scan``) → the Gmail message-id (with a
      ``:block_index`` suffix for each line item of a batched alert), NOT the
      natural-key hash.
    - **CSV** (``export-csv``) → a hex SHA-256 hash of the normalized natural key
      ``(source, posted_date, amount, description, last4)``, plus a per-row
      disambiguator (line-sequence index / running balance) so two genuinely
      distinct same-day/same-amount CSV rows do not collapse. The natural-key
      hash is used ONLY for export-csv.

    ``confirmed_ad_crm_id`` is a LOGICAL pointer at the confirmed CRM ad (the
    EspoCRM record id). It is a plain indexed column with NO foreign key — the ad
    lives in the MariaDB CRM, not here. ``NULL`` means the transaction is
    unmatched (that is the "unmatched" predicate used by the candidate queue).
    """

    __tablename__ = "bank_transactions"
    __table_args__ = (
        UniqueConstraint(
            "source", "source_txn_id", name="uq_bank_transactions_source_txn"
        ),
        CheckConstraint(
            "source IN ('email-scan', 'export-ofx', 'export-csv')",
            name="ck_bank_transactions_source",
        ),
        # Partial index for the unmatched-queue query: the popup/CLI candidate
        # lookup (matching/scorer.py, api /candidates) filters on unmatched rows
        # (confirmed_ad_crm_id IS NULL) within a txn_date window. Indexing only
        # the unmatched rows keeps it small and fast as matched history grows.
        Index(
            "idx_bank_txn_unmatched_date",
            "txn_date",
            postgresql_where=text("confirmed_ad_crm_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_txn_id: Mapped[str] = mapped_column(String(100), nullable=False)
    txn_date: Mapped[date] = mapped_column(Date, nullable=False)
    created_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    total_amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    payment_type: Mapped[str | None] = mapped_column(String(20))
    payment_ref_num: Mapped[str | None] = mapped_column(String(100))
    private_note: Mapped[str | None] = mapped_column(Text)
    doc_number: Mapped[str | None] = mapped_column(String(100))
    vendor_id: Mapped[str | None] = mapped_column(String(50))
    vendor_name: Mapped[str | None] = mapped_column(String(255))
    account_id: Mapped[str | None] = mapped_column(String(50))
    account_name: Mapped[str | None] = mapped_column(String(255))
    line_descriptions: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    raw_json: Mapped[dict | None] = mapped_column(JSONB)
    # Logical pointer at the confirmed CRM ad (EspoCRM record id). No FK: the ad
    # is in the MariaDB CRM, not this DB. NULL == unmatched.
    confirmed_ad_crm_id: Mapped[str | None] = mapped_column(String(50), index=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_in_db: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # DB-tracked ignore flag: when an ingested row's text matches an ACTIVE
    # ``ignore_memo_patterns`` entry it is a SAAS/vendor charge (not a
    # newspaper-ad payment). The row is flagged (never deleted) so the scorer
    # skips it as a reconcile candidate but the audit trail is preserved.
    # ``ignore_reason`` records which pattern matched (``ignore_pattern:<pattern>``).
    ignored: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    ignore_reason: Mapped[str | None] = mapped_column(Text)


class AdConfirmation(Base):
    """The relationship record linking a CRM ad to a bank txn and Gmail thread.

    The ad is referenced logically (``ad_crm_id`` = EspoCRM record id,
    ``ad_number`` = CRM ``adnumbernews``); both are plain indexed columns with NO
    foreign key because ad data lives in the MariaDB CRM. ``ad_crm_id`` is UNIQUE
    so a given CRM ad is confirmed at most once (idempotency). The
    ``bank_txn_id`` FK is a genuine same-database reference and is kept.
    """

    __tablename__ = "ad_confirmations"
    __table_args__ = (
        UniqueConstraint("ad_crm_id", name="uq_ad_confirmations_ad_crm_id"),
        Index("idx_ad_confirmations_ad_number", "ad_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Logical reference to the CRM ad (no FK — ad lives in the MariaDB CRM).
    ad_crm_id: Mapped[str] = mapped_column(String(50), nullable=False)
    ad_number: Mapped[str | None] = mapped_column(String(100))
    bank_txn_id: Mapped[int | None] = mapped_column(ForeignKey("bank_transactions.id"))
    gmail_thread_id: Mapped[str | None] = mapped_column(String(255))
    gmail_message_id: Mapped[str | None] = mapped_column(String(255))
    gmail_subject: Mapped[str | None] = mapped_column(Text)
    receipt_file_path: Mapped[str | None] = mapped_column(Text)
    receipt_url: Mapped[str | None] = mapped_column(Text)
    confirmed_by: Mapped[str | None] = mapped_column(String(100))
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    match_confidence: Mapped[str | None] = mapped_column(String(10))
    match_method: Mapped[str | None] = mapped_column(String(50))
    notes: Mapped[str | None] = mapped_column(Text)

    # Same-database relationship (valid FK). The ad has no ORM relationship — it
    # lives in the CRM and is referenced logically via ad_crm_id / ad_number.
    bank_txn: Mapped[BankTransaction | None] = relationship("BankTransaction")


class SyncLog(Base):
    """Audit trail for each confirmed-ctl ingestion/sync run."""

    __tablename__ = "confirmed_ctl_sync_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # The ingestion adapter that produced this run (e.g. ``email-scan``).
    source: Mapped[str | None] = mapped_column(String(50))
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lookback_days: Mapped[int | None] = mapped_column(Integer)
    txns_fetched: Mapped[int | None] = mapped_column(Integer)
    txns_new: Mapped[int | None] = mapped_column(Integer)
    txns_updated: Mapped[int | None] = mapped_column(Integer)
    auto_matched: Mapped[int | None] = mapped_column(Integer)
    errors: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)


class IgnoreMemoPattern(Base):
    """A DB-tracked ignore-string used to flag SAAS/vendor bank charges.

    ``pattern`` is a SHORT, stable substring (stored so trailing phone / location
    / date noise on a bank memo does not break the match). During ingest every
    ``active`` pattern is tested (case-insensitively) as a substring against a
    transaction's text fields (``vendor_name``/``private_note``/``line_descriptions``
    …); a hit sets ``bank_transactions.ignored = true`` so those recurring
    software-subscription charges never surface as reconcile candidates. Rows are
    flagged, never deleted — the audit trail is preserved.

    ``label`` is a human-friendly name for the vendor (e.g. ``"Fireworks AI
    (SAAS)"``). ``active`` lets a pattern be retired without deleting history.
    """

    __tablename__ = "ignore_memo_patterns"
    __table_args__ = (
        # Case-insensitive lookups on ``pattern`` (functional lower() index).
        Index("ix_ignore_memo_patterns_pattern_ci", text("lower(pattern)")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pattern: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
