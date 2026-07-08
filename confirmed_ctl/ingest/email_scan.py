"""BofA transaction-alert email-scan ingestion adapter.

This adapter reads (read-only) the ``info@perm-ads.com`` mailbox via the
service-account Gmail client and parses Bank of America transaction alerts
(all sent from ``onlinebanking@ealerts.bankofamerica.com``) into
``bank_transactions`` rows (``source='email-scan'``).

REAL FORMATS
------------
Every BofA alert in this mailbox is **HTML-only** (there is no ``text/plain``
part), so ``gmail.client.get_body_text`` converts the HTML body to plain text
(tags/entities stripped) BEFORE any label extraction happens here. Parsing is
label-based on that rendered text and tolerant of where HTML structure places
the label vs. its value (same line or the next line).

A subject-substring → schema routing table (case-insensitive) selects the
parser per alert. The known real subjects / schemas:

- ``SCHEMA-DEBITCARD-OVERLIMIT`` — "Account Alert: Debit/ATM Card Transaction
  Over Your Chosen Alert Limit". One transaction per email.
  Labels: ``Amount:`` ("$ 856.00"), ``Debit/ATM card:`` ("ending in - 5723"),
  ``Where:`` ("at MERCHANT-CITY ,ST"), ``Transaction type:`` ("PURCH W/O PIN"),
  ``When:`` ("on July 07, 2024").
- ``SCHEMA-DEBITCARD-USED`` — "Account Alert: Debit Card Used Online, by Phone
  or by Mail". SINGLE **or BATCHED** — the ``Account:``/``Amount:``/``Made at:``/
  ``On:`` block repeats once per transaction. Parsed by looping over every
  ``Account:`` block (no delimiter char). Amount may be immediately followed by a
  footnote superscript digit ("$ 15.001") which is NOT a quantity and is ignored.
- ``SCHEMA-ACH-WITHDRAWAL`` — "A withdrawal was made over the limit you set".
  ACH, NO card-ending field. Labels: ``Amount`` ("$1,487.50" — comma thousands,
  no space after ``$``), ``Type`` ("ELEC DRAFT (ACH)"), ``Account`` ("Ad Buys
  0353 - 0353", nickname + last4), ``Merchant`` ("AUDACY PURCHASE"),
  ``Transaction date`` ("June 13, 2025").
- ``SCHEMA-CURRENT-VARIANT`` — "A transaction occurred over the limit you set".
  The user's CURRENT live alert; we only have RENDERED fields (no raw HTML
  fixture), so its field regexes remain heuristic (# REFINE). Labels: ``Amount``,
  ``Debit/ATM card``, ``Merchant``, ``Transaction type``, ``Date``.

Two additional subjects are routed best-effort to the debit-card over-limit
parser (# REFINE): "Activity Alert: Electronic or Online Withdrawal Over Your
Chosen Alert Limit" and "Online transfer occurred over the limit you set".

Idempotency / ``source_txn_id`` derivation (see ``ingest.dedup``):

- Single-transaction alert → the Gmail ``message_id``.
- Each block of a BATCHED alert → ``f"{message_id}:{block_index}"``.

Both are fed through ``deterministic_source_txn_id(..., fitid=<id>)`` so re-scans
collapse on the ``uq_bank_transactions_source_txn`` unique constraint
(insert-conflict → SKIP). Both ``source`` and ``source_txn_id`` stay ``NOT NULL``.
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

# --- Schema keys -----------------------------------------------------------

SCHEMA_DEBITCARD_OVERLIMIT = "SCHEMA-DEBITCARD-OVERLIMIT"
SCHEMA_DEBITCARD_USED = "SCHEMA-DEBITCARD-USED"
SCHEMA_ACH_WITHDRAWAL = "SCHEMA-ACH-WITHDRAWAL"
SCHEMA_CURRENT_VARIANT = "SCHEMA-CURRENT-VARIANT"  # REFINE: no raw HTML sample

# --- Subject → schema routing table ----------------------------------------
#
# Ordered list of (subject substring, schema key). Matching is case-insensitive
# substring containment against the message subject. Order matters only if a
# subject could contain two substrings (none of the real ones overlap).
SUBJECT_ROUTES: tuple[tuple[str, str], ...] = (
    (
        "Debit/ATM Card Transaction Over Your Chosen Alert Limit",
        SCHEMA_DEBITCARD_OVERLIMIT,
    ),
    (
        "Debit Card Used Online, by Phone or by Mail",
        SCHEMA_DEBITCARD_USED,
    ),
    (
        "A withdrawal was made over the limit you set",
        SCHEMA_ACH_WITHDRAWAL,
    ),
    # Best-effort: same debit-card over-limit layout. # REFINE
    (
        "Electronic or Online Withdrawal Over Your Chosen Alert Limit",
        SCHEMA_DEBITCARD_OVERLIMIT,
    ),
    # Best-effort: the only still-recent subject; treat like the over-limit
    # debit-card layout until a raw sample confirms otherwise. # REFINE
    (
        "Online transfer occurred over the limit you set",
        SCHEMA_DEBITCARD_OVERLIMIT,
    ),
    # The user's CURRENT live alert — rendered fields only, no raw fixture. # REFINE
    (
        "A transaction occurred over the limit you set",
        SCHEMA_CURRENT_VARIANT,
    ),
)


def schema_for_subject(subject: str) -> str | None:
    """Return the schema key for a subject via case-insensitive substring match."""
    low = (subject or "").lower()
    for substr, schema in SUBJECT_ROUTES:
        if substr.lower() in low:
            return schema
    return None


# --- Per-schema label maps -------------------------------------------------
#
# Each label list is tried in order; the first that resolves wins. Labels are
# matched at the start of a rendered line (see ``_label_value``), tolerant of
# ``:``/``-`` separators and of the value living on the following line.

_SINGLE_SCHEMAS: dict[str, dict[str, tuple[str, ...]]] = {
    SCHEMA_DEBITCARD_OVERLIMIT: {
        "amount": ("Amount",),
        "last4": ("Debit/ATM card", "Debit/ATM Card"),
        "merchant": ("Where",),
        "txn_type": ("Transaction type",),
        "date": ("When",),
    },
    SCHEMA_ACH_WITHDRAWAL: {
        "amount": ("Amount",),
        "last4": ("Account",),  # nickname + last4, e.g. "Ad Buys 0353 - 0353"
        "merchant": ("Merchant",),
        "txn_type": ("Type",),
        "date": ("Transaction date",),
    },
    # REFINE: heuristic — validated only against rendered fields, not raw HTML.
    SCHEMA_CURRENT_VARIANT: {
        "amount": ("Amount",),
        "last4": ("Debit/ATM card", "Debit/ATM Card"),
        "merchant": ("Merchant",),
        "txn_type": ("Transaction type",),
        "date": ("Date",),
    },
}

# The batched debit-card-used block labels (one block per "Account:" line).
_USED_BLOCK_LABELS: dict[str, tuple[str, ...]] = {
    "amount": ("Amount",),
    "last4": ("Account",),  # "Debit card ending in 7625"
    "merchant": ("Made at",),
    "date": ("On",),
}
_USED_BLOCK_LABEL = "Account"

# --- Field-extraction regexes ----------------------------------------------

# A dollar amount tolerant of BOTH real formats:
#   "$ 856.00"  (space after $, no thousands comma)
#   "$1,487.50" (comma thousands, no space after $)
# The optional trailing ``\d*`` swallows a lone footnote superscript digit that
# BofA appends right after the cents (e.g. "$ 15.001" -> 15.00) so it is NOT
# mistaken for a quantity/count.
_AMOUNT_RE = re.compile(r"\$\s*(\d[\d,]*)(?:\.(\d{2}))?\d*")

# Card/account last-4. Prefers an explicit "ending in <digits>"; otherwise the
# trailing 4-digit run (handles "Ad Buys 0353 - 0353" -> 0353).
_ENDING_IN_RE = re.compile(r"ending in[\s:\-]*?(\d{3,})", re.IGNORECASE)
_TRAILING4_RE = re.compile(r"(\d{4})\D*$")
_ANY4_RE = re.compile(r"(\d{4})")

# Dates in the shapes BofA uses. # REFINE (kept broad for the heuristic variants)
_DATE_RE = re.compile(
    r"("
    r"\d{1,2}/\d{1,2}/\d{2,4}"                       # 07/07/2026 or 7/7/26
    r"|[A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4}"           # July 07, 2024
    r"|\d{4}-\d{2}-\d{2}"                            # 2026-07-07
    r")"
)

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

    ``amount`` is SIGNED: these alerts are debits/withdrawals, so it is negative.
    ``direction`` records the sign intent explicitly for downstream consumers.
    ``block_index`` is set only for the extra blocks of a BATCHED alert; it drives
    ``source_txn_id`` (``message_id:block_index``). A single-block alert leaves it
    ``None`` so its ``source_txn_id`` is just the bare ``message_id``.
    """

    posted_date: date
    amount: Decimal
    merchant: str | None
    last4: str | None
    txn_type: str | None
    message_id: str
    schema: str
    block_index: int | None = None
    direction: str = "debit"
    raw: dict = field(default_factory=dict)

    @property
    def source_txn_id(self) -> str:
        return email_scan_source_txn_id(self.message_id, self.block_index)


# --- Parsing helpers -------------------------------------------------------


def parse_amount(text: str) -> Decimal | None:
    """Return the dollar amount in ``text`` as a Decimal (unsigned).

    Handles both "$ 856.00" and "$1,487.50", strips ``$``/spaces/commas, and
    ignores a trailing lone footnote digit that follows the cents.
    """
    m = _AMOUNT_RE.search(text or "")
    if not m:
        return None
    dollars = m.group(1).replace(",", "")
    cents = m.group(2) or "00"
    try:
        return Decimal(f"{dollars}.{cents}")
    except InvalidOperation:
        return None


def parse_date(text: str, fallback: date | None = None) -> date | None:
    """Return the first parseable date in ``text``, else ``fallback``."""
    m = _DATE_RE.search(text or "")
    if m:
        token = m.group(1).strip().replace(".", "")
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(token, fmt).date()
            except ValueError:
                continue
    return fallback


def parse_last4(text: str) -> str | None:
    """Extract a 4-digit card/account tail from ``text``.

    Prefers "ending in <digits>" (taking its last 4); falls back to a trailing
    4-digit run, then any 4-digit run.
    """
    text = text or ""
    m = _ENDING_IN_RE.search(text)
    if m:
        return m.group(1)[-4:]
    m = _TRAILING4_RE.search(text.strip())
    if m:
        return m.group(1)
    m = _ANY4_RE.search(text)
    return m.group(1) if m else None


def _clean_merchant(value: str | None) -> str | None:
    """Tidy a merchant value: drop a leading ``at`` connector, collapse spaces.

    Keeps the embedded ``-CITY ,ST`` / phone tail intact (downstream concern).
    """
    if not value:
        return None
    merchant = re.sub(r"^\s*at\s+", "", value, flags=re.IGNORECASE)
    merchant = " ".join(merchant.split()).strip(" -:,")
    return merchant or None


def _clean_type(value: str | None) -> str | None:
    """Normalize a transaction-type value (collapse whitespace, upper-case)."""
    if not value:
        return None
    cleaned = " ".join(value.split()).upper().strip(" -:,")
    return cleaned or None


def _line_is_label(line: str, label: str) -> bool:
    """True if a stripped line is ``label`` immediately followed by a ``:``/``-``.

    A real separator is REQUIRED so a block label like ``Account`` does not
    falsely match a heading such as ``Account Alert: ...`` (the ``Account:``
    block delimiter always carries the separator).
    """
    return bool(
        re.match(rf"{re.escape(label)}\s*[:\-]", line.strip(), re.IGNORECASE)
    )


def _looks_like_label(line: str) -> bool:
    """Heuristic: a bare label line (short and ending in ':')."""
    s = line.strip()
    return s.endswith(":") and len(s) <= 40


def _label_value(text: str, labels: tuple[str, ...]) -> str | None:
    """Return the value for the first matching label in ``text``.

    Matches ``Label: value`` / ``Label - value`` / ``Label value`` at the start
    of a line (tolerant of HTML-rendered spacing). If the value is empty on the
    label line (label and value rendered on separate lines), the next non-empty,
    non-label line is used instead.
    """
    lines = (text or "").splitlines()
    for label in labels:
        pattern = re.compile(rf"^{re.escape(label)}\s*[:\-]?\s*(.*)$", re.IGNORECASE)
        for i, raw_line in enumerate(lines):
            line = raw_line.strip()
            if not line.lower().startswith(label.lower()):
                continue
            m = pattern.match(line)
            if not m:
                continue
            value = m.group(1).strip()
            if value:
                return value
            # Value likely on the following line(s).
            for nxt in lines[i + 1:]:
                nxt = nxt.strip()
                if not nxt:
                    continue
                if _looks_like_label(nxt):
                    break
                return nxt
    return None


# --- Schema parsers --------------------------------------------------------


def parse_single(
    text: str, schema: str, message_id: str, fallback_date: date | None
) -> EmailTxn | None:
    """Parse a single-transaction alert body using ``schema``'s label map.

    Returns ``None`` if the mandatory fields (amount + date) cannot be found.
    """
    labels = _SINGLE_SCHEMAS[schema]

    amount = parse_amount(_label_value(text, labels["amount"]) or text)
    posted = parse_date(_label_value(text, labels["date"]) or text, fallback_date)
    merchant = _clean_merchant(_label_value(text, labels.get("merchant", ())))
    last4 = parse_last4(_label_value(text, labels.get("last4", ())) or "")
    txn_type = _clean_type(_label_value(text, labels.get("txn_type", ())))

    if amount is None or posted is None:
        return None

    return EmailTxn(
        posted_date=posted,
        amount=-abs(amount),  # debit / withdrawal => negative
        merchant=merchant,
        last4=last4,
        txn_type=txn_type,
        message_id=message_id,
        schema=schema,
        block_index=None,
        raw={
            "schema": schema,
            "merchant_raw": _label_value(text, labels.get("merchant", ())),
            "amount_raw": str(amount),
        },
    )


def _split_blocks(text: str, block_label: str) -> list[str]:
    """Split ``text`` into blocks that each start at a ``block_label`` line.

    Used for BATCHED alerts: every ``Account:`` line begins a new transaction
    block that runs until the next ``Account:`` line (no delimiter character).
    """
    lines = (text or "").splitlines()
    starts = [i for i, ln in enumerate(lines) if _line_is_label(ln, block_label)]
    if not starts:
        return []
    blocks: list[str] = []
    for j, start in enumerate(starts):
        end = starts[j + 1] if j + 1 < len(starts) else len(lines)
        blocks.append("\n".join(lines[start:end]))
    return blocks


def parse_debitcard_used(
    text: str, message_id: str, fallback_date: date | None
) -> list[EmailTxn]:
    """Parse the SCHEMA-DEBITCARD-USED alert (single OR batched).

    Loops over every ``Account:`` block. A single-block email yields one txn
    with ``block_index=None`` (``source_txn_id == message_id``); a multi-block
    email yields one txn per block with ``block_index=i``
    (``source_txn_id == f"{message_id}:{i}"``).
    """
    blocks = _split_blocks(text, _USED_BLOCK_LABEL)
    if not blocks:
        return []

    multi = len(blocks) > 1
    txns: list[EmailTxn] = []
    for i, block in enumerate(blocks):
        amount = parse_amount(_label_value(block, _USED_BLOCK_LABELS["amount"]) or "")
        if amount is None:
            log.warning(
                "email-scan SCHEMA-DEBITCARD-USED block %s of %s had no amount",
                i,
                message_id,
            )
            continue
        last4 = parse_last4(_label_value(block, _USED_BLOCK_LABELS["last4"]) or "")
        merchant = _clean_merchant(_label_value(block, _USED_BLOCK_LABELS["merchant"]))
        posted = parse_date(
            _label_value(block, _USED_BLOCK_LABELS["date"]) or "", fallback_date
        )
        txns.append(
            EmailTxn(
                posted_date=posted or fallback_date or date.today(),
                amount=-abs(amount),
                merchant=merchant,
                last4=last4,
                txn_type=None,
                message_id=message_id,
                schema=SCHEMA_DEBITCARD_USED,
                block_index=(i if multi else None),
                raw={"schema": SCHEMA_DEBITCARD_USED, "block_raw": block},
            )
        )
    return txns


def parse_message(
    text: str, subject: str, message_id: str, fallback_date: date | None
) -> list[EmailTxn]:
    """Route a message body to its schema parser. Returns 0..N transactions."""
    schema = schema_for_subject(subject)
    if schema is None:
        log.warning("email-scan unroutable subject for %s: %r", message_id, subject)
        return []
    if schema == SCHEMA_DEBITCARD_USED:
        return parse_debitcard_used(text, message_id, fallback_date)
    txn = parse_single(text, schema, message_id, fallback_date)
    return [txn] if txn else []


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
        private_note=f"BofA alert ({txn.schema})",
        line_descriptions=[merchant] if merchant else None,
        raw_json={
            "message_id": txn.message_id,
            "schema": txn.schema,
            "block_index": txn.block_index,
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
    """Build a date-bounded Gmail query for one alert subject."""
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
    """Search + fetch + parse every routed alert subject. Returns all txns.

    Pure of any DB access — the network side is fully isolated here so parsing
    and insertion can be tested offline. Each route in ``SUBJECT_ROUTES`` is
    queried; the returned messages are parsed with the route's schema.
    """
    from ..gmail import client as gmail_client

    all_txns: list[EmailTxn] = []
    for subject, schema in SUBJECT_ROUTES:
        query = build_query(subject, lookback_days, today)
        log.info("email-scan query [%s]: %s", schema, query)
        for stub in gmail_client.search_messages(service, query):
            msg_id = stub["id"]
            message = gmail_client.get_message(service, msg_id)
            body = gmail_client.get_body_text(message)
            fallback = _message_date(message)
            parsed = parse_message(body, subject, msg_id, fallback)
            if parsed:
                all_txns.extend(parsed)
            else:
                log.warning("email-scan parse miss [%s] for message %s", schema, msg_id)
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
