"""SQLAlchemy models for confirmed-ctl.

Tables owned by confirmed-ctl: ``bank_transactions``, ``ad_confirmations``,
``confirmed_ctl_sync_log`` (see docs/ARCHITECTURE-SPEC.md Section 3).

``ad_purchases`` is an existing CRM table. It is modelled here only for the
columns confirmed-ctl reads/relates to; confirmed-ctl never creates or migrates
it (see ``EXTERNAL_TABLES``).
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Tables that live in the shared CRM database but are NOT owned/migrated by
# confirmed-ctl. Alembic's autogenerate is configured to ignore these.
EXTERNAL_TABLES = frozenset({"ad_purchases"})


class Base(DeclarativeBase):
    pass


class AdPurchase(Base):
    """Existing CRM newspaper-ad purchase record (read-only for confirmed-ctl)."""

    __tablename__ = "ad_purchases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ad_number: Mapped[str | None] = mapped_column(String(100))
    client_name: Mapped[str | None] = mapped_column(String(255))
    newspaper_name: Mapped[str | None] = mapped_column(String(255))
    run_date: Mapped[date | None] = mapped_column(Date)
    expected_charge_date: Mapped[date | None] = mapped_column(Date)
    expected_amount: Mapped[float | None] = mapped_column(Numeric(10, 2))


class BankTransaction(Base):
    """A QBO Purchase/BillPayment synced into the local database."""

    __tablename__ = "bank_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    qbo_id: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    sync_token: Mapped[str | None] = mapped_column(String(20))
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
    confirmed_ad_id: Mapped[int | None] = mapped_column(
        ForeignKey("ad_purchases.id", ondelete="SET NULL")
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_in_db: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AdConfirmation(Base):
    """The relationship record linking an ad to a bank txn and Gmail thread."""

    __tablename__ = "ad_confirmations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ad_id: Mapped[int] = mapped_column(
        ForeignKey("ad_purchases.id"), nullable=False, unique=True
    )
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

    ad: Mapped[AdPurchase] = relationship("AdPurchase")
    bank_txn: Mapped[BankTransaction | None] = relationship("BankTransaction")


class SyncLog(Base):
    """Audit trail for each confirmed-ctl QBO sync run."""

    __tablename__ = "confirmed_ctl_sync_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
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
