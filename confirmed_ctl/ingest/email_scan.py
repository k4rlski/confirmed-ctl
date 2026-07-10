"""BofA transaction-alert email-scan ingestion adapter.

This adapter reads (read-only) the impersonated mailbox (``GMAIL_IMPERSONATE``,
default ``karl@perm-ads.com``) via the service-account Gmail client and parses
Bank of America transaction alerts (all sent from
``onlinebanking@ealerts.bankofamerica.com``) into ``bank_transactions`` rows
(``source='email-scan'``).

WHICH MAILBOX / WHERE THE ALERTS LIVE (operational caveat)
----------------------------------------------------------
The default mailbox is ``karl@perm-ads.com``: it holds every BofA alert in its
**durable INBOX** (not Trash) and also receives Paul's ``info@`` vendor
ad-confirmation emails that this tool searches by the CRM ad number
(``adnumbernews``). ``info@perm-ads.com`` is only the delivery address — a Gmail
filter there auto-sends BofA alerts to **Trash**, which Gmail purges after ~30
days (so scanning ``info@`` would require a **daily** run for completeness). The
mailbox is configurable via ``GMAIL_IMPERSONATE`` (``info@perm-ads.com`` / an
admin mailbox are alternatives). Regardless of mailbox, the Gmail client lists
with ``includeSpamTrash=True`` **defensively** (see
``gmail.client.search_messages``) so any trashed alert is still found. Gmail
settings/filters are never modified.

REAL FORMATS (bs4 table-cell pairing)
-------------------------------------
Every BofA alert here is **HTML-only**. The modern alerts render each field as a
two-cell table row: a LABEL ``<td>`` followed by a VALUE ``<td>`` (the value is
usually wrapped in ``<b>``). This adapter parses the RAW HTML with BeautifulSoup
and pairs adjacent cell texts (label -> value) — it does NOT rely on a flattened
"label: value" text rendering, which mis-associated fields. Internal whitespace
in values is collapsed (merchant strings carry irregular multiple spaces, e.g.
``"SA EXPRESS NEWS ADV   -SAN ANTONIO  ,TX"``).

A subject-substring -> schema routing table (case-insensitive) selects the
per-alert field map. The supported schemas (all sender
``onlinebanking@ealerts.bankofamerica.com``):

- ``SCHEMA-CARD`` — debit/ATM card transaction over the limit. Current subject
  "A transaction occurred over the limit you set"; older backfill subject
  "Account Alert: Debit/ATM Card Transaction Over Your Chosen Alert Limit".
  Fields: Amount ("$2,000.00"), Debit/ATM card ("ending in 5723" — no dash; also
  older "ending in - 5723"), Merchant, Transaction type ("PURCH W/O PIN"), Date
  ("July 08, 2026"). HIGHEST VALUE for ad matching. (Real .eml fixture.)
- ``SCHEMA-ACH-WITHDRAWAL`` — ACH withdrawal over the limit. Current subject
  "A withdrawal was made over the limit you set"; older backfill "Activity Alert:
  Electronic or Online Withdrawal Over Your Chosen Alert Limit". Fields: Amount,
  Type ("ELEC DRAFT (ACH)"), Account ("Ad Buys 0353 - 0353" nickname + last4),
  Merchant ("COXMEDIAGROUP    WEBPAYMENT"), Transaction date. (Real .eml fixture.)
- ``SCHEMA-TRANSFER`` — online transfer over the limit. Current subject "Online
  transfer occurred over the limit you set"; older backfill "Activity Alert:
  Online Transfer Over Your Chosen Alert Limit". Fields: Account ("ending in
  0353"), Amount, Transaction date. NO merchant/type — tolerant (needs only
  amount + date + last4). # REFINE: no raw HTML fixture yet (assumes the modern
  two-column table like CARD/ACH).
- ``SCHEMA-DEBITCARD-USED`` — debit card used online/phone/mail. Current subject
  "Your debit card was used"; older backfill "Debit Card Used Online, by Phone
  or by Mail". SINGLE **or BATCHED**: an ``Account``/``Amount``/``Made at``/``On``
  block repeats once per transaction; parsed by looping over every ``Account``
  block. # REFINE: no raw HTML fixture yet (structure assumed from older text).

Idempotency / ``source_txn_id`` derivation (see ``ingest.dedup``):

- Single-transaction alert -> the Gmail ``message_id``.
- Each block of a BATCHED alert -> ``f"{message_id}:{block_index}"``.

Iteration is MESSAGE-LEVEL (one logical row per message id; batched blocks add a
``:i`` suffix). Thread-grouped same-day alerts each carry a distinct message id,
so they each become a distinct row. Both ``source`` and ``source_txn_id`` stay
``NOT NULL`` and re-scans collapse on ``uq_bank_transactions_source_txn``
(insert-conflict -> SKIP).
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

# All BofA transaction alerts arrive from this single sender.
BOFA_SENDER = "onlinebanking@ealerts.bankofamerica.com"

# --- Schema keys -----------------------------------------------------------

SCHEMA_CARD = "SCHEMA-CARD"
SCHEMA_ACH_WITHDRAWAL = "SCHEMA-ACH-WITHDRAWAL"
SCHEMA_TRANSFER = "SCHEMA-TRANSFER"  # REFINE: no raw HTML fixture yet
SCHEMA_DEBITCARD_USED = "SCHEMA-DEBITCARD-USED"  # REFINE: no raw HTML fixture yet

# --- Subject -> schema routing table ---------------------------------------
#
# Ordered list of (subject substring, schema key). Matching is case-insensitive
# substring containment against the RAW ``Subject`` header. Both the current and
# older (backfill) subjects for each schema are listed. Order is irrelevant here
# because no substring of one schema is contained in another schema's subject.
SUBJECT_ROUTES: tuple[tuple[str, str], ...] = (
    # CARD — current + older backfill.
    ("a transaction occurred over the limit you set", SCHEMA_CARD),
    ("Debit/ATM Card Transaction Over Your Chosen Alert Limit", SCHEMA_CARD),
    # ACH WITHDRAWAL — current + older backfill.
    ("a withdrawal was made over the limit you set", SCHEMA_ACH_WITHDRAWAL),
    ("Electronic or Online Withdrawal Over Your Chosen Alert Limit", SCHEMA_ACH_WITHDRAWAL),
    # TRANSFER — current + older backfill. # REFINE (no raw HTML fixture).
    ("online transfer occurred over the limit you set", SCHEMA_TRANSFER),
    ("Online Transfer Over Your Chosen Alert Limit", SCHEMA_TRANSFER),
    # DEBIT-CARD-USED — current + older backfill. # REFINE (no raw HTML fixture).
    ("your debit card was used", SCHEMA_DEBITCARD_USED),
    ("Debit Card Used Online, by Phone or by Mail", SCHEMA_DEBITCARD_USED),
)


def schema_for_subject(subject: str) -> str | None:
    """Return the schema key for a subject via case-insensitive substring match."""
    low = (subject or "").lower()
    for substr, schema in SUBJECT_ROUTES:
        if substr.lower() in low:
            return schema
    return None


# --- Per-schema field -> candidate label maps ------------------------------
#
# Keys are lower-cased data-table labels as they appear in the LABEL ``<td>``.
# Each field lists candidate labels tried in order (first present wins), so the
# modern label and any older-format label are both accepted for backfill.

_SINGLE_SCHEMAS: dict[str, dict[str, tuple[str, ...]]] = {
    SCHEMA_CARD: {
        "amount": ("amount",),
        "last4": ("debit/atm card",),
        "merchant": ("merchant", "where"),
        "txn_type": ("transaction type",),
        "date": ("date", "when"),
    },
    SCHEMA_ACH_WITHDRAWAL: {
        "amount": ("amount",),
        "last4": ("account",),  # nickname + last4, e.g. "Ad Buys 0353 - 0353"
        "merchant": ("merchant",),
        "txn_type": ("type",),
        "date": ("transaction date", "date"),
    },
    # Transfer alerts carry no merchant/type; only amount + account + date. # REFINE
    SCHEMA_TRANSFER: {
        "amount": ("amount",),
        "last4": ("account",),  # e.g. "ending in 0353"
        "date": ("transaction date", "date"),
    },
}

# The batched debit-card-used block labels (one block per "Account" row). # REFINE
_USED_BLOCK_LABELS: dict[str, tuple[str, ...]] = {
    "amount": ("amount",),
    "last4": ("account",),  # "Debit card ending in 7625"
    "merchant": ("made at", "merchant"),
    "date": ("on", "date"),
}
_USED_BLOCK_LABEL = "account"

# --- Field-extraction regexes ----------------------------------------------

# A dollar amount tolerant of BOTH real formats:
#   "$ 856.00"  (space after $, no thousands comma)
#   "$1,487.50" (comma thousands, no space after $)
# The optional trailing ``\d*`` swallows a lone footnote superscript digit that
# BofA appends right after the cents (e.g. "$ 15.001" -> 15.00) so it is NOT
# mistaken for a quantity/count.
_AMOUNT_RE = re.compile(r"\$\s*(\d[\d,]*)(?:\.(\d{2}))?\d*")

# Card/account last-4. Prefers an explicit "ending in <digits>" (no dash OR an
# older "ending in - 5723"); otherwise the trailing 4-digit run (handles
# "Ad Buys 0353 - 0353" -> 0353), then any 4-digit run.
_ENDING_IN_RE = re.compile(r"ending in[\s:\-]*?(\d{3,})", re.IGNORECASE)
_TRAILING4_RE = re.compile(r"(\d{4})\D*$")
_ANY4_RE = re.compile(r"(\d{4})")

# Dates in the shapes BofA uses.
_DATE_RE = re.compile(
    r"("
    r"\d{1,2}/\d{1,2}/\d{2,4}"                       # 07/07/2026 or 7/7/26
    r"|[A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4}"           # July 08, 2026
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
    # Gmail thread id of the source BofA alert (from the message stub's
    # ``threadId``). Persisted to ``bank_transactions.bofa_gmail_thread_id`` so
    # the modal can deep-link the alert email. ``None`` when the caller did not
    # supply it (e.g. older parse paths / tests).
    thread_id: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def source_txn_id(self) -> str:
        return email_scan_source_txn_id(self.message_id, self.block_index)


# --- HTML table-cell pairing (bs4) -----------------------------------------


def _collapse_ws(value: str | None) -> str:
    """Collapse ALL runs of internal whitespace to a single space; strip ends."""
    if not value:
        return ""
    return " ".join(value.split())


def extract_pairs(html: str) -> list[tuple[str, str]]:
    """Pair the two-column data-table cells of a BofA alert: (label, value).

    BofA renders each field as a table row with exactly two direct ``<td>``
    children — a LABEL cell and a VALUE cell (value usually in ``<b>``). This
    walks every ``<tr>``, keeps rows with exactly two direct ``<td>`` children,
    and returns ``(label, value)`` in document order with internal whitespace
    collapsed. Layout rows (single cell, or nested tables) are skipped; junk
    two-cell rows are harmless because callers look up specific labels.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    pairs: list[tuple[str, str]] = []
    for tr in soup.find_all("tr"):
        cells = tr.find_all("td", recursive=False)
        if len(cells) != 2:
            continue
        label = _collapse_ws(cells[0].get_text(" ", strip=True))
        value = _collapse_ws(cells[1].get_text(" ", strip=True))
        if label and value:
            pairs.append((label, value))
    return pairs


def _label_key(label: str) -> str:
    """Normalize a label cell to its lookup key (lower-cased, trailing ':' off)."""
    return _collapse_ws(label).lower().rstrip(":").strip()


def pairs_to_map(pairs: list[tuple[str, str]]) -> dict[str, str]:
    """Collapse ordered (label, value) pairs into a first-wins label->value map."""
    out: dict[str, str] = {}
    for label, value in pairs:
        out.setdefault(_label_key(label), value)
    return out


def _first(field_map: dict[str, str], labels: tuple[str, ...]) -> str | None:
    """Return the first present label's value from ``field_map``."""
    for label in labels:
        if label in field_map:
            return field_map[label]
    return None


# --- Parsing helpers -------------------------------------------------------


def parse_amount(text: str | None) -> Decimal | None:
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


def parse_date(text: str | None, fallback: date | None = None) -> date | None:
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


def parse_last4(text: str | None) -> str | None:
    """Extract a 4-digit card/account tail from ``text``.

    Prefers "ending in <digits>" (taking its last 4; tolerates a dash such as
    "ending in - 5723"); falls back to a trailing 4-digit run, then any run.
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


# --- Schema parsers --------------------------------------------------------


def parse_paired(
    html: str,
    schema: str,
    message_id: str,
    fallback_date: date | None,
    thread_id: str | None = None,
) -> EmailTxn | None:
    """Parse a single-transaction alert from paired HTML cells using ``schema``.

    Fail-closed: only the labeled data-cell value is used, never a whole-body
    scan (which could grab the alert LIMIT figure or a stray date). Returns
    ``None`` if the mandatory fields (amount + date) cannot be found.
    """
    spec = _SINGLE_SCHEMAS[schema]
    field_map = pairs_to_map(extract_pairs(html))

    amount_raw = _first(field_map, spec["amount"])
    amount = parse_amount(amount_raw) if amount_raw is not None else None
    date_raw = _first(field_map, spec["date"])
    posted = parse_date(date_raw, fallback_date) if date_raw is not None else fallback_date
    merchant = _clean_merchant(_first(field_map, spec.get("merchant", ())))
    last4 = parse_last4(_first(field_map, spec.get("last4", ())))
    txn_type = _clean_type(_first(field_map, spec.get("txn_type", ())))

    if amount is None:
        log.warning(
            "email-scan %s parse-miss (no amount cell) for message %s",
            schema,
            message_id,
        )
        return None
    if posted is None:
        log.warning(
            "email-scan %s parse-miss (no date cell) for message %s",
            schema,
            message_id,
        )
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
        thread_id=thread_id,
        raw={
            "schema": schema,
            "merchant_raw": _first(field_map, spec.get("merchant", ())),
            "amount_raw": amount_raw,
        },
    )


def _split_pair_blocks(
    pairs: list[tuple[str, str]], block_label: str
) -> list[dict[str, str]]:
    """Split ordered pairs into per-transaction blocks starting at ``block_label``.

    Used for BATCHED alerts: every ``Account`` row begins a new block that runs
    until the next ``Account`` row. Each block is a first-wins label->value map.
    """
    blocks: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for label, value in pairs:
        key = _label_key(label)
        if key == block_label:
            current = {}
            blocks.append(current)
        if current is not None:
            current.setdefault(key, value)
    return blocks


def parse_debitcard_used(
    html: str,
    message_id: str,
    fallback_date: date | None,
    thread_id: str | None = None,
) -> list[EmailTxn]:
    """Parse the SCHEMA-DEBITCARD-USED alert (single OR batched). # REFINE.

    Loops over every ``Account`` block. A single-block email yields one txn with
    ``block_index=None`` (``source_txn_id == message_id``); a multi-block email
    yields one txn per block with ``block_index=i``
    (``source_txn_id == f"{message_id}:{i}"``). Fail-closed per block: a block
    with no amount or no resolvable date is skipped (never fabricated). No raw
    HTML fixture exists yet, so the block structure is assumed from the older
    text layout.
    """
    blocks = _split_pair_blocks(extract_pairs(html), _USED_BLOCK_LABEL)
    if not blocks:
        return []

    multi = len(blocks) > 1
    txns: list[EmailTxn] = []
    for i, block in enumerate(blocks):
        amount_raw = _first(block, _USED_BLOCK_LABELS["amount"])
        amount = parse_amount(amount_raw) if amount_raw is not None else None
        if amount is None:
            log.warning(
                "email-scan SCHEMA-DEBITCARD-USED parse-miss (no amount) "
                "block %s of %s",
                i,
                message_id,
            )
            continue
        last4 = parse_last4(_first(block, _USED_BLOCK_LABELS["last4"]))
        merchant = _clean_merchant(_first(block, _USED_BLOCK_LABELS["merchant"]))
        date_raw = _first(block, _USED_BLOCK_LABELS["date"])
        posted = (
            parse_date(date_raw, fallback_date) if date_raw is not None else fallback_date
        )
        # No fabricated date: skip the block rather than invent date.today().
        if posted is None:
            log.warning(
                "email-scan SCHEMA-DEBITCARD-USED parse-miss (no date) "
                "block %s of %s",
                i,
                message_id,
            )
            continue
        txns.append(
            EmailTxn(
                posted_date=posted,
                amount=-abs(amount),
                merchant=merchant,
                last4=last4,
                txn_type=None,
                message_id=message_id,
                schema=SCHEMA_DEBITCARD_USED,
                block_index=(i if multi else None),
                thread_id=thread_id,
                raw={"schema": SCHEMA_DEBITCARD_USED, "block_index": i},
            )
        )
    return txns


def parse_message(
    html: str,
    subject: str,
    message_id: str,
    fallback_date: date | None,
    thread_id: str | None = None,
) -> list[EmailTxn]:
    """Route a message (raw HTML) to its schema parser. Returns 0..N txns.

    Classification is by case-insensitive SUBSTRING match of the RAW ``Subject``
    header against ``SUBJECT_ROUTES``. ``thread_id`` (the Gmail alert thread) is
    threaded onto every produced ``EmailTxn`` so it lands on
    ``bank_transactions.bofa_gmail_thread_id`` at insert.
    """
    schema = schema_for_subject(subject)
    if schema is None:
        log.warning("email-scan unroutable subject for %s: %r", message_id, subject)
        return []
    if schema == SCHEMA_DEBITCARD_USED:
        return parse_debitcard_used(html, message_id, fallback_date, thread_id)
    txn = parse_paired(html, schema, message_id, fallback_date, thread_id)
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
        bofa_gmail_thread_id=txn.thread_id,
        raw_json={
            "message_id": txn.message_id,
            "thread_id": txn.thread_id,
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


def insert_transactions(
    session,
    txns: list[EmailTxn],
    ignore_patterns: list[tuple[str, str | None]] | None = None,
) -> tuple[int, int]:
    """Insert parsed transactions, skipping conflicts. Returns (inserted, skipped).

    Idempotent: a pre-check against ``(source, source_txn_id)`` skips already
    stored rows, and a per-row SAVEPOINT catches any residual unique-constraint
    violation (race / within-batch duplicate) as a SKIP rather than an error.
    Portable across Postgres and SQLite (uses only ORM primitives).

    ``ignore_patterns`` (loaded once per run by the caller via
    ``ingest.ignore.load_active_ignore_patterns``) flags SAAS/vendor rows: any
    stored row whose text matches an active pattern gets ``ignored=true`` +
    ``ignore_reason`` so the scorer skips it. The row is stored regardless (flag,
    don't drop).
    """
    from sqlalchemy.exc import IntegrityError

    from ..db.models import BankTransaction
    from .ignore import apply_ignore_flags

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
            model = _to_model(txn)
            if ignore_patterns:
                apply_ignore_flags(model, ignore_patterns)
            session.add(model)
            session.flush()
            savepoint.commit()
            inserted += 1
        except IntegrityError:
            savepoint.rollback()
            skipped += 1
    return inserted, skipped


# --- Gmail orchestration ---------------------------------------------------


def build_query(lookback_days: int, today: date | None = None) -> str:
    """Build the broad, date-bounded scan query.

    A single SENDER query (``from:<BofA sender>``) bounded by ``after:<epoch>``
    replaces the brittle per-route ``subject:"..."`` phrase queries, which
    under-match (Gmail's phrase operator drops alerts). Every returned message is
    then classified by its RAW subject substring. ``after:`` uses epoch SECONDS
    (unambiguous across time zones).
    """
    today = today or datetime.now(timezone.utc).date()
    after = today - timedelta(days=max(0, lookback_days))
    epoch = int(
        datetime(after.year, after.month, after.day, tzinfo=timezone.utc).timestamp()
    )
    return f"from:{BOFA_SENDER} after:{epoch}"


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
    """Search + fetch + classify + parse BofA alerts. Returns all txns.

    MESSAGE-LEVEL iteration: one broad sender query is run, then EACH returned
    message is fetched, classified by its raw ``Subject`` header, and parsed from
    its raw HTML body. Distinct message ids (even within one thread) yield
    distinct rows. Pure of any DB access so parsing/insertion can be tested
    offline.
    """
    from ..gmail import client as gmail_client

    query = build_query(lookback_days, today)
    log.info("email-scan query: %s", query)

    all_txns: list[EmailTxn] = []
    for stub in gmail_client.search_messages(service, query):
        msg_id = stub["id"]
        # The message stub carries {id, threadId}; capture the alert thread so
        # the modal can deep-link the BofA email (persisted per row at insert).
        thread_id = stub.get("threadId")
        message = gmail_client.get_message(service, msg_id)
        headers = gmail_client.get_headers(message)
        subject = headers.get("subject", "")
        html = gmail_client.get_html_body(message)
        fallback = _message_date(message)
        parsed = parse_message(html, subject, msg_id, fallback, thread_id)
        if parsed:
            all_txns.extend(parsed)
        else:
            log.warning(
                "email-scan parse miss for message %s (subject=%r)", msg_id, subject
            )
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
        # Load the active ignore patterns ONCE per run so SAAS/vendor rows are
        # flagged (ignored=true) as they are stored (flag, don't drop).
        from .ignore import load_active_ignore_patterns

        ignore_patterns = load_active_ignore_patterns(session)
        inserted, skipped = insert_transactions(
            session, txns, ignore_patterns=ignore_patterns
        )
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
