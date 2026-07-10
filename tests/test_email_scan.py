"""Offline tests for the BofA email-scan ingestion adapter.

No live Gmail, no Postgres, no network. Gmail access is faked by monkeypatching
the ``confirmed_ctl.gmail.client`` module functions, and the DB is a tiny
in-memory ``FakeSession`` that emulates only the ORM primitives the adapter
uses (``query().filter_by().first()``, ``add``, ``begin_nested``, ``flush``,
``commit``) and enforces the ``(source, source_txn_id)`` uniqueness that the
real ``uq_bank_transactions_source_txn`` constraint provides.

The CARD and ACH fixtures are the REAL BofA alert HTML bodies (saved under
``tests/fixtures/``), so the bs4 table-cell parser is exercised against the
exact markup BofA sends. TRANSFER and DEBIT-CARD-USED have no raw sample yet, so
they use small BofA-shaped synthetic HTML (# REFINE).
"""

import pathlib
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from confirmed_ctl.gmail import client as gmail_client
from confirmed_ctl.ingest.dedup import email_scan_source_txn_id
from confirmed_ctl.ingest.email_scan import (
    BOFA_SENDER,
    SCHEMA_ACH_WITHDRAWAL,
    SCHEMA_CARD,
    SCHEMA_DEBITCARD_USED,
    SCHEMA_TRANSFER,
    build_query,
    extract_pairs,
    insert_transactions,
    pairs_to_map,
    parse_amount,
    parse_debitcard_used,
    parse_last4,
    parse_message,
    parse_paired,
    run_email_scan,
    scan_messages,
    schema_for_subject,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
CARD_HTML = (FIXTURES / "card_transaction_over_limit.html").read_text(encoding="utf-8")
ACH_HTML = (FIXTURES / "ach_withdrawal_over_limit.html").read_text(encoding="utf-8")

# --- Real subjects (raw Subject header text) -------------------------------

SUBJ_CARD = "A transaction occurred over the limit you set"
SUBJ_CARD_OLD = (
    "Account Alert: Debit/ATM Card Transaction Over Your Chosen Alert Limit"
)
SUBJ_ACH = "A withdrawal was made over the limit you set"
SUBJ_ACH_OLD = (
    "Activity Alert: Electronic or Online Withdrawal Over Your Chosen Alert Limit"
)
SUBJ_TRANSFER = "Online transfer occurred over the limit you set"
SUBJ_TRANSFER_OLD = "Activity Alert: Online Transfer Over Your Chosen Alert Limit"
SUBJ_USED = "Your debit card was used"
SUBJ_USED_OLD = "Account Alert: Debit Card Used Online, by Phone or by Mail"

# --- Synthetic BofA-shaped HTML for the # REFINE schemas -------------------


def _row(label: str, value: str) -> str:
    """A BofA-style two-cell data row (label <td> + value <td> in <b>)."""
    return f"<tr><td>{label}</td><td><b>{value}</b></td></tr>"


TRANSFER_HTML = (
    "<html><body><table>"
    + _row("Amount", "$1,200.00")
    + _row("Account", "ending in 0353")
    + _row("Transaction date", "July 08, 2026")
    + "</table></body></html>"
)

USED_SINGLE_HTML = (
    "<html><body><table>"
    + _row("Account", "Debit card ending in 7625")
    + _row("Amount", "$ 15.001")  # footnote superscript "1" -> ignored
    + _row("Made at", "BUFFER PUBLISH PRO MO   -+14152955970  ,CA")
    + _row("On", "August 13, 2024")
    + "</table></body></html>"
)

USED_BATCH_HTML = (
    "<html><body><table>"
    + _row("Account", "Debit card ending in 7625")
    + _row("Amount", "$ 15.00")
    + _row("Made at", "BUFFER PUBLISH PRO MO -+14152955970 ,CA")
    + _row("On", "August 13, 2024")
    + _row("Account", "Debit card ending in 7625")
    + _row("Amount", "$ 42.99")
    + _row("Made at", "AMAZON MKTPL AMZN.COM -SEATTLE ,WA")
    + _row("On", "August 13, 2024")
    + _row("Account", "Debit card ending in 7625")
    + _row("Amount", "$ 5.00")
    + _row("Made at", "OPENAI CHATGPT -+14158799686 ,CA")
    + _row("On", "August 12, 2024")
    + _row("Account", "Debit card ending in 7625")
    + _row("Amount", "$ 120.50")
    + _row("Made at", "META ADS -MENLO PARK ,CA")
    + _row("On", "August 12, 2024")
    + _row("Account", "Debit card ending in 7625")
    + _row("Amount", "$ 8.25")
    + _row("Made at", "SPOTIFY USA -NEW YORK ,NY")
    + _row("On", "August 11, 2024")
    + "</table></body></html>"
)

# Older-format CARD HTML: dash last4 + Where/When labels (backfill tolerance).
CARD_OLD_HTML = (
    "<html><body><table>"
    + _row("Amount", "$ 856.00")
    + _row("Debit/ATM card", "ending in - 5723")
    + _row("Where", "at THE PHILADELPHIA INQUI-PHILADELPHIA ,PA")
    + _row("Transaction type", "PURCH W/O PIN")
    + _row("When", "July 07, 2024")
    + "</table></body></html>"
)

MSG_CARD = "msgCARD111"
MSG_ACH = "msgACH333"
MSG_TRANSFER = "msgXFER222"
MSG_USED_SINGLE = "msgUSED1s"
MSG_USED_BATCH = "msgUSEDbat"


# --- Subject routing -------------------------------------------------------


def test_subject_routing_current_and_backfill():
    assert schema_for_subject(SUBJ_CARD) == SCHEMA_CARD
    assert schema_for_subject(SUBJ_CARD_OLD) == SCHEMA_CARD
    assert schema_for_subject(SUBJ_ACH) == SCHEMA_ACH_WITHDRAWAL
    assert schema_for_subject(SUBJ_ACH_OLD) == SCHEMA_ACH_WITHDRAWAL
    assert schema_for_subject(SUBJ_TRANSFER) == SCHEMA_TRANSFER
    assert schema_for_subject(SUBJ_TRANSFER_OLD) == SCHEMA_TRANSFER
    assert schema_for_subject(SUBJ_USED) == SCHEMA_DEBITCARD_USED
    assert schema_for_subject(SUBJ_USED_OLD) == SCHEMA_DEBITCARD_USED


def test_subject_routing_is_case_insensitive_substring():
    # Real subjects arrive with extra prefixes/suffixes; substring match wins.
    assert schema_for_subject(SUBJ_CARD.upper()) == SCHEMA_CARD
    assert schema_for_subject("Fwd: " + SUBJ_ACH + " (info@)") == SCHEMA_ACH_WITHDRAWAL
    assert schema_for_subject("Your statement is ready") is None
    assert schema_for_subject("") is None


# --- Amount / last4 cleaners -----------------------------------------------


def test_amount_cleaner_dual_formats_and_footnote():
    assert parse_amount("$ 856.00") == Decimal("856.00")       # space, no comma
    assert parse_amount("$1,487.50") == Decimal("1487.50")     # comma, no space
    assert parse_amount("$2,000.00") == Decimal("2000.00")
    assert parse_amount("$255.00") == Decimal("255.00")
    assert parse_amount("$ 15.001") == Decimal("15.00")        # footnote "1" ignored
    assert parse_amount("$ 15.00") == Decimal("15.00")
    assert parse_amount("no dollar here") is None
    assert parse_amount(None) is None


def test_last4_extraction_variants():
    assert parse_last4("ending in 5723") == "5723"             # no dash (current)
    assert parse_last4("ending in - 5723") == "5723"           # older dash form
    assert parse_last4("Debit card ending in 7625") == "7625"
    assert parse_last4("Ad Buys 0353 - 0353") == "0353"        # trailing run
    assert parse_last4("ending in 0353") == "0353"
    assert parse_last4(None) is None


# --- bs4 table-cell pairing (real CARD fixture) ----------------------------


def test_extract_pairs_bs4_cell_pairing_and_whitespace_collapse():
    field_map = pairs_to_map(extract_pairs(CARD_HTML))
    assert field_map["amount"] == "$2,000.00"
    assert field_map["debit/atm card"] == "ending in 5723"     # no dash
    assert field_map["transaction type"] == "PURCH W/O PIN"
    assert field_map["date"] == "July 08, 2026"
    # Merchant carries irregular multiple spaces in the raw HTML; collapsed here.
    assert field_map["merchant"] == "SA EXPRESS NEWS ADV -SAN ANTONIO ,TX"
    assert "  " not in field_map["merchant"]


# --- CARD (real fixture) ---------------------------------------------------


def test_card_field_extraction_real_fixture():
    txn = parse_paired(CARD_HTML, SCHEMA_CARD, MSG_CARD, fallback_date=None)
    assert txn is not None
    assert txn.amount == Decimal("-2000.00")   # debit => negative (signed)
    assert txn.amount < 0
    assert txn.last4 == "5723"
    assert txn.merchant == "SA EXPRESS NEWS ADV -SAN ANTONIO ,TX"
    assert txn.txn_type == "PURCH W/O PIN"
    assert txn.posted_date == date(2026, 7, 8)
    assert txn.schema == SCHEMA_CARD
    # Single alert => source_txn_id is the bare message id.
    assert txn.block_index is None
    assert txn.source_txn_id == MSG_CARD
    assert txn.source_txn_id == email_scan_source_txn_id(MSG_CARD)


def test_card_old_format_backfill_labels_and_dash_last4():
    txn = parse_paired(CARD_OLD_HTML, SCHEMA_CARD, MSG_CARD, fallback_date=None)
    assert txn is not None
    assert txn.amount == Decimal("-856.00")
    assert txn.last4 == "5723"                 # older "ending in - 5723"
    assert txn.merchant == "THE PHILADELPHIA INQUI-PHILADELPHIA ,PA"  # "at " dropped
    assert txn.txn_type == "PURCH W/O PIN"
    assert txn.posted_date == date(2024, 7, 7)  # via "When" backfill label


# --- ACH WITHDRAWAL (real fixture) -----------------------------------------


def test_ach_withdrawal_extraction_real_fixture():
    txn = parse_paired(ACH_HTML, SCHEMA_ACH_WITHDRAWAL, MSG_ACH, fallback_date=None)
    assert txn is not None
    assert txn.amount == Decimal("-255.00")
    assert txn.txn_type == "ELEC DRAFT (ACH)"
    assert txn.last4 == "0353"                 # nickname + last4 "Ad Buys 0353 - 0353"
    assert txn.merchant == "COXMEDIAGROUP WEBPAYMENT"   # collapsed multi-space
    assert txn.posted_date == date(2026, 7, 8)
    assert txn.schema == SCHEMA_ACH_WITHDRAWAL


# --- TRANSFER (# REFINE synthetic) -----------------------------------------


def test_transfer_extraction_tolerant_no_merchant_or_type():
    txn = parse_paired(TRANSFER_HTML, SCHEMA_TRANSFER, MSG_TRANSFER, fallback_date=None)
    assert txn is not None
    assert txn.amount == Decimal("-1200.00")
    assert txn.last4 == "0353"
    assert txn.posted_date == date(2026, 7, 8)
    assert txn.merchant is None                # transfers carry no merchant
    assert txn.txn_type is None                # ...and no type
    assert txn.schema == SCHEMA_TRANSFER


# --- DEBIT-CARD-USED (# REFINE synthetic; single + batched) ----------------


def test_used_single_footnote_and_single_id():
    txns = parse_debitcard_used(
        USED_SINGLE_HTML, MSG_USED_SINGLE, fallback_date=date(2024, 8, 13)
    )
    assert len(txns) == 1
    t = txns[0]
    assert t.amount == Decimal("-15.00")       # footnote "1" ignored, not 15.001
    assert t.last4 == "7625"
    assert t.merchant == "BUFFER PUBLISH PRO MO -+14152955970 ,CA"
    assert t.posted_date == date(2024, 8, 13)
    # Single block => bare message id (no :i suffix).
    assert t.block_index is None
    assert t.source_txn_id == MSG_USED_SINGLE


def test_used_batched_yields_n_rows_with_distinct_ids():
    txns = parse_debitcard_used(
        USED_BATCH_HTML, MSG_USED_BATCH, fallback_date=date(2024, 8, 13)
    )
    assert len(txns) == 5
    assert [t.amount for t in txns] == [
        Decimal("-15.00"),
        Decimal("-42.99"),
        Decimal("-5.00"),
        Decimal("-120.50"),
        Decimal("-8.25"),
    ]
    assert all(t.last4 == "7625" for t in txns)
    assert "AMAZON MKTPL" in txns[1].merchant
    assert txns[2].posted_date == date(2024, 8, 12)
    assert [t.source_txn_id for t in txns] == [
        f"{MSG_USED_BATCH}:0",
        f"{MSG_USED_BATCH}:1",
        f"{MSG_USED_BATCH}:2",
        f"{MSG_USED_BATCH}:3",
        f"{MSG_USED_BATCH}:4",
    ]
    assert len({t.source_txn_id for t in txns}) == 5


# --- Fail-closed parsing ---------------------------------------------------

# Amount cell missing but the alert LIMIT figure sits elsewhere in the body: a
# whole-body scan would grab the limit; the cell-pairing parser must not.
CARD_NO_AMOUNT_HTML = (
    "<html><body>"
    "<p>Your chosen alert limit is $ 500.00</p>"
    "<table>"
    + _row("Debit/ATM card", "ending in 5723")
    + _row("Merchant", "SOME MERCHANT -CITY ,ST")
    + _row("Date", "July 08, 2026")
    + "</table></body></html>"
)

# Amount present, date cell missing, and a stray date in a footer paragraph.
CARD_NO_DATE_HTML = (
    "<html><body>"
    + "<table>"
    + _row("Amount", "$ 856.00")
    + _row("Debit/ATM card", "ending in 5723")
    + _row("Merchant", "SOME MERCHANT -CITY ,ST")
    + "</table>"
    + "<p>Message generated January 01, 2000.</p>"
    + "</body></html>"
)


def test_fail_closed_missing_amount_cell_skips_record():
    txn = parse_paired(
        CARD_NO_AMOUNT_HTML, SCHEMA_CARD, MSG_CARD, fallback_date=date(2026, 7, 8)
    )
    assert txn is None  # not a wrong -$500.00 (the alert LIMIT)


def test_fail_closed_missing_date_cell_no_stray_body_date():
    txn = parse_paired(CARD_NO_DATE_HTML, SCHEMA_CARD, MSG_CARD, fallback_date=None)
    assert txn is None  # never the stray "January 01, 2000"


def test_fail_closed_missing_date_cell_uses_fallback_not_stray():
    txn = parse_paired(
        CARD_NO_DATE_HTML, SCHEMA_CARD, MSG_CARD, fallback_date=date(2026, 7, 8)
    )
    assert txn is not None
    assert txn.posted_date == date(2026, 7, 8)
    assert txn.posted_date != date(2000, 1, 1)


# Batched block with an amount but no "On" date and no fallback: skip the block,
# never fabricate date.today().
USED_BLOCK_NO_DATE_HTML = (
    "<html><body><table>"
    + _row("Account", "Debit card ending in 7625")
    + _row("Amount", "$ 15.00")
    + _row("Made at", "BUFFER PUBLISH PRO MO -+14152955970 ,CA")
    + "</table></body></html>"
)


def test_fail_closed_batched_missing_date_skips_block_no_today():
    txns = parse_debitcard_used(
        USED_BLOCK_NO_DATE_HTML, MSG_USED_SINGLE, fallback_date=None
    )
    assert txns == []  # no fabricated date.today(); the block is skipped


# --- parse_message routing (classification by subject substring) -----------


def test_parse_message_routes_by_subject_substring():
    assert len(parse_message(CARD_HTML, SUBJ_CARD, MSG_CARD, None)) == 1
    assert len(parse_message(ACH_HTML, SUBJ_ACH, MSG_ACH, None)) == 1
    assert len(parse_message(TRANSFER_HTML, SUBJ_TRANSFER, MSG_TRANSFER, None)) == 1
    assert len(parse_message(USED_BATCH_HTML, SUBJ_USED, MSG_USED_BATCH, None)) == 5
    # Older backfill subjects route the same way.
    assert len(parse_message(ACH_HTML, SUBJ_ACH_OLD, MSG_ACH, None)) == 1
    assert parse_message("<html></html>", "Unknown subject", "m", None) == []


# --- Query building --------------------------------------------------------


def test_build_query_is_broad_sender_and_epoch_bounded():
    from datetime import datetime, timezone

    q = build_query(lookback_days=2, today=date(2026, 7, 8))
    assert q.startswith(f"from:{BOFA_SENDER} ")
    assert 'subject:"' not in q  # NOT a brittle subject phrase query
    # after: uses epoch SECONDS for the lookback start (2026-07-06 00:00 UTC).
    epoch = int(datetime(2026, 7, 6, tzinfo=timezone.utc).timestamp())
    assert f"after:{epoch}" in q


# --- Fake DB session (no Postgres) -----------------------------------------


class _FakeSavepoint:
    def __init__(self, session):
        self.session = session

    def commit(self):
        pass

    def rollback(self):
        self.session.pending = []


class _FakeQuery:
    def __init__(self, store):
        self.store = store
        self._filters = {}

    def filter_by(self, **kw):
        self._filters = kw
        return self

    def first(self):
        key = (self._filters.get("source"), self._filters.get("source_txn_id"))
        return self.store.get(key)


class FakeSession:
    """Minimal in-memory stand-in enforcing (source, source_txn_id) uniqueness."""

    def __init__(self):
        self.store = {}
        self.pending = []
        self.sync_logs = []

    def query(self, *args, **kwargs):
        return _FakeQuery(self.store)

    def add(self, obj):
        self.pending.append(obj)

    def begin_nested(self):
        return _FakeSavepoint(self)

    def flush(self):
        for obj in self.pending:
            stid = getattr(obj, "source_txn_id", None)
            if stid is None:
                self.sync_logs.append(obj)
                continue
            key = (obj.source, stid)
            if key in self.store:
                self.pending = []
                raise IntegrityError("duplicate", {}, Exception("duplicate"))
            self.store[key] = obj
        self.pending = []

    def commit(self):
        self.flush()

    def rollback(self):
        self.pending = []


# --- Insert idempotency + message-level distinct rows ----------------------


def test_insert_batched_inserts_then_skips():
    session = FakeSession()
    txns = parse_debitcard_used(USED_BATCH_HTML, MSG_USED_BATCH, date(2024, 8, 13))

    inserted, skipped = insert_transactions(session, txns)
    assert (inserted, skipped) == (5, 0)
    assert len(session.store) == 5

    # Re-inserting the SAME parsed transactions is a no-op (conflict-skip).
    inserted2, skipped2 = insert_transactions(session, txns)
    assert (inserted2, skipped2) == (0, 5)
    assert len(session.store) == 5


def test_same_message_parsed_twice_is_one_logical_row():
    session = FakeSession()
    first = parse_paired(CARD_HTML, SCHEMA_CARD, MSG_CARD, date(2026, 7, 8))
    again = parse_paired(CARD_HTML, SCHEMA_CARD, MSG_CARD, date(2026, 7, 8))

    insert_transactions(session, [first])
    insert_transactions(session, [again])
    assert len(session.store) == 1  # dedup on message-id-derived source_txn_id


def test_thread_grouped_messages_yield_distinct_rows():
    """Same-thread, same-day alerts (distinct message ids) => distinct rows."""
    session = FakeSession()
    # Two messages that share a threadId but have different message ids.
    a = parse_paired(CARD_HTML, SCHEMA_CARD, "threadA:msg1", date(2026, 7, 8))
    b = parse_paired(CARD_HTML, SCHEMA_CARD, "threadA:msg2", date(2026, 7, 8))
    assert a.source_txn_id != b.source_txn_id
    inserted, skipped = insert_transactions(session, [a, b])
    assert (inserted, skipped) == (2, 0)
    assert len(session.store) == 2  # message-level iteration => one row per msg id


# --- includeSpamTrash on the real search_messages -------------------------


class _RecordingList:
    def __init__(self, recorder):
        self._recorder = recorder

    def list(self, **kwargs):
        self._recorder.append(kwargs)

        class _Exec:
            def execute(self_inner):
                # Empty result stops pagination after one call.
                return {"messages": [], "nextPageToken": None}

        return _Exec()


class _RecordingUsers:
    def __init__(self, recorder):
        self._messages = _RecordingList(recorder)

    def messages(self):
        return self._messages


class _RecordingService:
    def __init__(self):
        self.calls = []
        self._users = _RecordingUsers(self.calls)

    def users(self):
        return self._users


def test_search_messages_passes_include_spam_trash():
    svc = _RecordingService()
    list(gmail_client.search_messages(svc, "from:x after:1"))
    assert svc.calls, "messages().list was never called"
    assert svc.calls[0].get("includeSpamTrash") is True


# --- End-to-end run (faked Gmail, message-level) ---------------------------


def _b64(text: str) -> dict:
    import base64

    return {"data": base64.urlsafe_b64encode(text.encode()).decode()}


@pytest.fixture
def fake_gmail(monkeypatch):
    # One broad sender query returns ALL messages; each is classified by subject.
    messages = {
        MSG_CARD: {
            "id": MSG_CARD, "threadId": "tCARD", "internalDate": "1751846400000",
            "subject": SUBJ_CARD, "html": CARD_HTML,
        },
        MSG_ACH: {
            "id": MSG_ACH, "threadId": "tACH", "internalDate": "1751846400000",
            "subject": SUBJ_ACH, "html": ACH_HTML,
        },
        MSG_TRANSFER: {
            "id": MSG_TRANSFER, "threadId": "tXFER", "internalDate": "1751846400000",
            "subject": SUBJ_TRANSFER, "html": TRANSFER_HTML,
        },
        MSG_USED_BATCH: {
            "id": MSG_USED_BATCH, "threadId": "tUSED", "internalDate": "1723507200000",
            "subject": SUBJ_USED, "html": USED_BATCH_HTML,
        },
    }

    def fake_search(service, query, max_results=2000):
        return iter([{"id": m["id"], "threadId": m["threadId"]} for m in messages.values()])

    def fake_get_message(service, message_id, fmt="full"):
        return messages[message_id]

    def fake_get_headers(message):
        return {"subject": message["subject"]}

    def fake_get_html_body(message):
        return message["html"]

    monkeypatch.setattr(gmail_client, "search_messages", fake_search)
    monkeypatch.setattr(gmail_client, "get_message", fake_get_message)
    monkeypatch.setattr(gmail_client, "get_headers", fake_get_headers)
    monkeypatch.setattr(gmail_client, "get_html_body", fake_get_html_body)
    return object()  # a placeholder "service"


def test_scan_messages_collects_all_schemas(fake_gmail):
    txns = scan_messages(fake_gmail, lookback_days=7)
    # 1 card + 1 ACH + 1 transfer + 5 batched used = 8
    assert len(txns) == 8
    by_schema = {}
    for t in txns:
        by_schema[t.schema] = by_schema.get(t.schema, 0) + 1
    assert by_schema[SCHEMA_CARD] == 1
    assert by_schema[SCHEMA_ACH_WITHDRAWAL] == 1
    assert by_schema[SCHEMA_TRANSFER] == 1
    assert by_schema[SCHEMA_DEBITCARD_USED] == 5


def test_run_email_scan_is_idempotent(fake_gmail):
    session = FakeSession()

    result = run_email_scan(session, lookback_days=7, service=fake_gmail)
    assert result["found"] == 8
    assert result["inserted"] == 8
    assert result["skipped"] == 0
    assert len(session.sync_logs) == 1
    assert session.sync_logs[0].source == "email-scan"

    # Second pass over the same emails inserts nothing (idempotent).
    result2 = run_email_scan(session, lookback_days=7, service=fake_gmail)
    assert result2["found"] == 8
    assert result2["inserted"] == 0
    assert result2["skipped"] == 8
    assert len(session.store) == 8
    assert len(session.sync_logs) == 2


# --- BofA alert Gmail thread-id capture ------------------------------------


def test_parse_message_threads_alert_thread_id():
    # parse_message threads the alert Gmail threadId onto every produced txn.
    card = parse_message(CARD_HTML, SUBJ_CARD, MSG_CARD, None, thread_id="tCARD")
    assert card and card[0].thread_id == "tCARD"
    used = parse_message(
        USED_BATCH_HTML, SUBJ_USED, MSG_USED_BATCH, None, thread_id="tUSED"
    )
    assert used and all(t.thread_id == "tUSED" for t in used)
    # Omitted thread id stays None (older parse paths / tests).
    assert parse_message(CARD_HTML, SUBJ_CARD, MSG_CARD, None)[0].thread_id is None


def test_scan_captures_bofa_thread_id_onto_model(fake_gmail):
    # scan_messages captures each stub's threadId; the model build persists it to
    # bank_transactions.bofa_gmail_thread_id (+ raw_json["thread_id"]).
    txns = scan_messages(fake_gmail, lookback_days=7)
    assert all(t.thread_id for t in txns)
    card = next(t for t in txns if t.schema == SCHEMA_CARD)
    assert card.thread_id == "tCARD"
    used = [t for t in txns if t.schema == SCHEMA_DEBITCARD_USED]
    assert used and all(t.thread_id == "tUSED" for t in used)

    session = FakeSession()
    insert_transactions(session, txns)
    stored = list(session.store.values())
    card_row = next(r for r in stored if r.raw_json.get("schema") == SCHEMA_CARD)
    assert card_row.bofa_gmail_thread_id == "tCARD"
    assert card_row.raw_json["thread_id"] == "tCARD"
