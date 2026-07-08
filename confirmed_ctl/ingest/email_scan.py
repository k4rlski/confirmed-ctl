"""BofA transaction-alert email-scan ingestion adapter.

This adapter reads (read-only) the ``info@perm-ads.com`` mailbox via the
service-account Gmail client and parses Bank of America debit-card transaction
alerts into ``bank_transactions`` rows (``source='email-scan'``).

Two Gmail query "missions", both date-bounded by a lookback window:

- **Type A — per-transaction (TRUSTED)**
  ``subject:"A transaction occurred over the limit you set"``.
  One transaction per email. Parsed fields: amount, card last4, merchant/memo,
  transaction type, date.

- **Type B — batched (LESS trusted)**
  ``subject:"Your debit card was used"``.
  May contain MULTIPLE transactions in one email; each line item is parsed.

Idempotency / ``source_txn_id`` derivation (see ``ingest.dedup``):

- Type A → the Gmail ``message_id``.
- Type B → ``f"{message_id}:{line_index}"`` per line item.

Both are fed through ``deterministic_source_txn_id(..., fitid=<id>)`` so re-scans
collapse on the ``uq_bank_transactions_source_txn`` unique constraint
(insert-conflict → SKIP).

PARSER NOTE (refinement points): the exact wording/layout of BofA alert bodies
must be confirmed against real sanitized samples. The field-extraction regexes
below (``_LABELS``, ``PRICE_RE``, ``LAST4_RE``, ``TXN_TYPE_RE``, ``DATE_RE`` and
``parse_type_b`` line splitting) are deliberately isolated and tolerant so they
are easy to adjust once samples land. Each is flagged with ``# REFINE:``.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from .. import settings
from .dedup import email_scan_source_txn_id

log = logging.getLogger("confirmed-ctl.email-scan")

# --- Gmail query "missions" ------------------------------------------------

# The two alert subjects. Kept as exact phrases (quoted in the Gmail query).
SUBJECT_TYPE_A = "A transaction occurred over the limit you set"
SUBJECT_TYPE_B = "Your debit card was used"

TYPE_A = "A"
TYPE_B = "B"

# --- Field-extraction regexes (REFINE against real samples) ----------------

# A dollar amount, e.g. "$1,234.56" or "$50". # REFINE
PRICE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{2})?)")

# Card last-4, e.g. "ending in 1234" / "ending in: 1234". # REFINE
LAST4_RE = re.compile(r"ending in[:\s]*?(\d{4})", re.IGNORECASE)

# Transaction type token, e.g. "PURCH W/O PIN", "PURCHASE", "ATM WITHDRAWAL". # REFINE
TXN_TYPE_RE = re.compile(
    r"(PURCH(?:ASE)?(?:\s+W/?O?\s*PIN)?|ATM\s+WITHDRAWAL|POS\s+PURCHASE|"
    r"RECURRING(?:\s+PAYMENT)?)",
    re.IGNORECASE,
)

# Dates in the common shapes BofA uses. # REFINE
DATE_RE = re.compile(
    r"("
    r"\d{1,2}/\d{1,2}/\d{2,4}"                       # 07/07/2026 or 7/7/26
    r"|[A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4}"           # July 07, 2026
    r"|\d{4}-\d{2}-\d{2}"                            # 2026-07-07
    r")"
)

# Label -> the text captured to the end of that line. Used for Type A where each
# field sits on its own labelled line. Order matters (first match wins). # REFINE
_LABELS: dict[str, tuple[str, ...]] = {
    "amount": ("amount",),
    "merchant": ("where", "merchant", "description", "payee"),
    "last4": ("account", "card"),
    "txn_type": ("transaction type", "type"),
    "date": ("date", "when"),
}

_DATE_FORMATS = (
    "%m/%d/%Y",
    "%m/%d/%y",
    "%Y-%m-%d",
    "%B %d, %Y",
    "%B %d %Y",
    "%b %d, %Y",
    "%b %d %Y",
)


@dataclass
class EmailTxn:
    """Normalized transaction parsed from a BofA alert email.

    ``amount`` is SIGNED: debit-card alerts are debits, so it is negative.
    ``direction`` records the sign intent explicitly for downstream consumers.
    """

    posted_date: date
    amount: Decimal
    merchant: str | None
    last4: str | None
    txn_type: str | None
    message_id: str
    mission_type: str
    line_index: int | None = None
    direction: str = "debit"
    raw: dict = field(default_factory=dict)

    @property
    def source_txn_id(self) -> str:
        return email_scan_source_txn_id(self.message_id, self.line_index)


# --- Parsing helpers -------------------------------------------------------


def parse_amount(text: str) -> Decimal | None:
    """Return the first dollar amount in ``text`` as a Decimal (unsigned)."""
    m = PRICE_RE.search(text or "")
    if not m:
        return None
    try:
        return Decimal(m.group(1).replace(",", ""))
    except InvalidOperation:
        return None


def parse_date(text: str, fallback: date | None = None) -> date | None:
    """Return the first parseable date in ``text``, else ``fallback``."""
    m = DATE_RE.search(text or "")
    if m:
        token = m.group(1).strip().replace(".", "")
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(token, fmt).date()
            except ValueError:
                continue
    return fallback


def parse_last4(text: str) -> str | None:
    m = LAST4_RE.search(text or "")
    return m.group(1) if m else None


def parse_txn_type(text: str) -> str | None:
    m = TXN_TYPE_RE.search(text or "")
    return " ".join(m.group(1).upper().split()) if m else None


def _label_value(body: str, labels: tuple[str, ...]) -> str | None:
    """Return the trailing value on the first line matching any of ``labels``.

    Matches ``Label: value`` (or ``Label - value``). Tolerant of extra spacing.
    """
    for raw_line in (body or "").splitlines():
        line = raw_line.strip()
        for label in labels:
            m = re.match(
                rf"{re.escape(label)}\s*[:\-]\s*(.+)$", line, re.IGNORECASE
            )
            if m and m.group(1).strip():
                return m.group(1).strip()
    return None


def parse_type_a(body: str, message_id: str, fallback_date: date | None) -> EmailTxn | None:
    """Parse a single-transaction (Type A) BofA alert body.

    Uses labelled-line extraction first, then falls back to a whole-body regex
    sweep for any field the labels missed. Returns ``None`` if the mandatory
    fields (date + amount) cannot be determined.
    """
    amount_txt = _label_value(body, _LABELS["amount"]) or body
    amount = parse_amount(amount_txt)

    merchant = _label_value(body, _LABELS["merchant"])

    last4_txt = _label_value(body, _LABELS["last4"]) or body
    last4 = parse_last4(last4_txt)

    type_txt = _label_value(body, _LABELS["txn_type"]) or body
    txn_type = parse_txn_type(type_txt)

    date_txt = _label_value(body, _LABELS["date"]) or body
    posted = parse_date(date_txt, fallback_date)

    if amount is None or posted is None:
        return None

    return EmailTxn(
        posted_date=posted,
        amount=-abs(amount),  # debit-card alert => debit => negative
        merchant=merchant,
        last4=last4,
        txn_type=txn_type,
        message_id=message_id,
        mission_type=TYPE_A,
        line_index=None,
        raw={
            "subject": SUBJECT_TYPE_A,
            "merchant_raw": merchant,
            "amount_raw": str(amount),
        },
    )


def parse_type_b(body: str, message_id: str, fallback_date: date | None) -> list[EmailTxn]:
    """Parse a batched (Type B) BofA alert body into per-line transactions.

    Heuristic: every line that contains a dollar amount is treated as one line
    item. The merchant is the remaining text on that line (with the amount, a
    leading ``at``/``-`` connector, and any trailing date stripped). Line index
    is 0-based over the *emitted* line items (drives ``source_txn_id``). # REFINE
    """
    txns: list[EmailTxn] = []
    for raw_line in (body or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        amount = parse_amount(line)
        if amount is None:
            continue

        last4 = parse_last4(line)
        txn_type = parse_txn_type(line)
        posted = parse_date(line, fallback_date)

        merchant = PRICE_RE.sub("", line)
        merchant = DATE_RE.sub("", merchant)
        merchant = re.sub(r"\bending in[:\s]*\d{4}\b", "", merchant, flags=re.IGNORECASE)
        merchant = re.sub(r"^\s*(?:at|-|on|:)\s*", "", merchant, flags=re.IGNORECASE)
        merchant = " ".join(merchant.split()).strip(" -:,") or None

        txns.append(
            EmailTxn(
                posted_date=posted or fallback_date or date.today(),
                amount=-abs(amount),
                merchant=merchant,
                last4=last4,
                txn_type=txn_type,
                message_id=message_id,
                mission_type=TYPE_B,
                line_index=len(txns),
                raw={"subject": SUBJECT_TYPE_B, "line_raw": line},
            )
        )
    return txns


# --- DB insert (idempotent, conflict-skip) ---------------------------------


def _to_model(txn: EmailTxn):
    """Build a ``BankTransaction`` ORM object from a parsed ``EmailTxn``."""
    from ..db.models import BankTransaction

    merchant = (txn.merchant or "")[:255] or None
    return BankTransaction(
        source="email-scan",
        source_txn_id=txn.source_txn_id,
        txn_date=txn.posted_date,
        total_amount=txn.amount,
        payment_type=(txn.txn_type or "")[:20] or None,
        payment_ref_num=txn.last4,
        vendor_name=merchant,
        private_note=f"BofA alert (type {txn.mission_type})",
        line_descriptions=[merchant] if merchant else None,
        raw_json={
            "message_id": txn.message_id,
            "mission_type": txn.mission_type,
            "line_index": txn.line_index,
            "merchant": txn.merchant,
            "last4": txn.last4,
            "txn_type": txn.txn_type,
            "amount": str(txn.amount),
            "direction": txn.direction,
            "posted_date": txn.posted_date.isoformat(),
            **txn.raw,
        },
    )


def insert_transactions(session, txns: list[EmailTxn]) -> tuple[int, int]:
    """Insert parsed transactions, skipping conflicts. Returns (inserted, skipped).

    Idempotent: a pre-check against ``(source, source_txn_id)`` skips already
    stored rows, and a per-row SAVEPOINT catches any residual unique-constraint
    violation (race / within-batch duplicate) as a SKIP rather than an error.
    Portable across Postgres and SQLite (uses only ORM primitives).
    """
    from sqlalchemy.exc import IntegrityError

    from ..db.models import BankTransaction

    inserted = 0
    skipped = 0
    for txn in txns:
        existing = (
            session.query(BankTransaction.id)
            .filter_by(source="email-scan", source_txn_id=txn.source_txn_id)
            .first()
        )
        if existing is not None:
            skipped += 1
            continue

        savepoint = session.begin_nested()
        try:
            session.add(_to_model(txn))
            session.flush()
            savepoint.commit()
            inserted += 1
        except IntegrityError:
            savepoint.rollback()
            skipped += 1
    return inserted, skipped


# --- Gmail orchestration ---------------------------------------------------


def build_query(subject: str, lookback_days: int, today: date | None = None) -> str:
    """Build a date-bounded Gmail query for one mission subject."""
    today = today or datetime.now(timezone.utc).date()
    after = today - timedelta(days=max(0, lookback_days))
    return f'subject:"{subject}" after:{after:%Y/%m/%d}'


def _message_date(message: dict) -> date | None:
    """Best-effort posting date from the Gmail ``internalDate`` (epoch ms)."""
    internal = message.get("internalDate")
    if internal:
        try:
            return datetime.fromtimestamp(
                int(internal) / 1000, tz=timezone.utc
            ).date()
        except (ValueError, OverflowError, OSError):
            return None
    return None


def scan_messages(service, lookback_days: int, today: date | None = None) -> list[EmailTxn]:
    """Search + fetch + parse both missions. Returns all parsed transactions.

    Pure of any DB access — the network side is fully isolated here so parsing
    and insertion can be tested offline.
    """
    from ..gmail import client as gmail_client

    missions = (
        (TYPE_A, SUBJECT_TYPE_A),
        (TYPE_B, SUBJECT_TYPE_B),
    )
    all_txns: list[EmailTxn] = []
    for mission_type, subject in missions:
        query = build_query(subject, lookback_days, today)
        log.info("email-scan mission %s query: %s", mission_type, query)
        for stub in gmail_client.search_messages(service, query):
            msg_id = stub["id"]
            message = gmail_client.get_message(service, msg_id)
            body = gmail_client.get_body_text(message)
            fallback = _message_date(message)
            if mission_type == TYPE_A:
                txn = parse_type_a(body, msg_id, fallback)
                if txn:
                    all_txns.append(txn)
                else:
                    log.warning("email-scan Type A parse miss for message %s", msg_id)
            else:
                parsed = parse_type_b(body, msg_id, fallback)
                if parsed:
                    all_txns.extend(parsed)
                else:
                    log.warning("email-scan Type B parse miss for message %s", msg_id)
    return all_txns


def run_email_scan(session, lookback_days: int | None = None, service=None) -> dict:
    """Run a full email-scan pass and record a SyncLog. Returns a counts dict.

    Args:
        session: an open SQLAlchemy session (caller owns commit/close).
        lookback_days: window; defaults to ``settings.EMAIL_SCAN_LOOKBACK_DAYS``.
        service: an optional pre-built Gmail service (tests inject a fake); when
            ``None`` a real service-account service is built lazily.
    """
    from ..db.models import SyncLog

    lookback_days = (
        settings.EMAIL_SCAN_LOOKBACK_DAYS if lookback_days is None else lookback_days
    )
    started = time.monotonic()
    errors: str | None = None
    found = inserted = skipped = 0

    try:
        if service is None:
            from ..gmail.client import get_gmail_service

            service = get_gmail_service()
        txns = scan_messages(service, lookback_days)
        found = len(txns)
        inserted, skipped = insert_transactions(session, txns)
    except Exception as exc:  # record failure in the sync log, then re-raise
        errors = f"{type(exc).__name__}: {exc}"
        log.exception("email-scan run failed")

    duration_ms = int((time.monotonic() - started) * 1000)
    session.add(
        SyncLog(
            source="email-scan",
            lookback_days=lookback_days,
            txns_fetched=found,
            txns_new=inserted,
            txns_updated=skipped,
            errors=errors,
            duration_ms=duration_ms,
        )
    )
    session.commit()

    if errors:
        raise RuntimeError(errors)

    return {
        "source": "email-scan",
        "lookback_days": lookback_days,
        "found": found,
        "inserted": inserted,
        "skipped": skipped,
    }
