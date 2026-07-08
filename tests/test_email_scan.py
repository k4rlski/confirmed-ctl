"""Offline tests for the BofA email-scan ingestion adapter.

No live Gmail, no Postgres, no network. Gmail access is faked by monkeypatching
the ``confirmed_ctl.gmail.client`` module functions, and the DB is a tiny
in-memory ``FakeSession`` that emulates only the ORM primitives the adapter
uses (``query().filter_by().first()``, ``add``, ``begin_nested``, ``flush``,
``commit``) and enforces the ``(source, source_txn_id)`` uniqueness that the
real ``uq_bank_transactions_source_txn`` constraint provides.

Fixture bodies below are the REAL BofA alert layouts (rendered to plain text as
``gmail.client.get_body_text`` would produce from the HTML-only bodies).
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from confirmed_ctl.gmail.client import get_body_text, html_to_text
from confirmed_ctl.ingest.dedup import email_scan_source_txn_id
from confirmed_ctl.ingest.email_scan import (
    SCHEMA_ACH_WITHDRAWAL,
    SCHEMA_CURRENT_VARIANT,
    SCHEMA_DEBITCARD_OVERLIMIT,
    SCHEMA_DEBITCARD_USED,
    build_query,
    insert_transactions,
    parse_amount,
    parse_debitcard_used,
    parse_last4,
    parse_message,
    parse_single,
    run_email_scan,
    scan_messages,
    schema_for_subject,
)

# --- Real subjects ---------------------------------------------------------

SUBJ_OVERLIMIT = (
    "Account Alert: Debit/ATM Card Transaction Over Your Chosen Alert Limit"
)
SUBJ_USED = "Account Alert: Debit Card Used Online, by Phone or by Mail"
SUBJ_ACH = "A withdrawal was made over the limit you set"
SUBJ_CURRENT = "A transaction occurred over the limit you set"
SUBJ_ELEC = (
    "Activity Alert: Electronic or Online Withdrawal Over Your Chosen Alert Limit"
)
SUBJ_TRANSFER = "Online transfer occurred over the limit you set"

# --- Real fixture bodies (post HTML->text) ---------------------------------

# SCHEMA-DEBITCARD-OVERLIMIT — per transaction; "$ 856.00" (space, no comma).
OVERLIMIT_BODY = """Bank of America

Account Alert: Debit/ATM Card Transaction Over Your Chosen Alert Limit

Amount: $ 856.00
Debit/ATM card: ending in - 5723
Where: at THE PHILADELPHIA INQUI-PHILADELPHIA ,PA
Transaction type: PURCH W/O PIN
When: on July 07, 2024

View details in Online Banking.
"""

# SCHEMA-DEBITCARD-USED — SINGLE; note the footnote superscript "1" on the amount.
USED_SINGLE_BODY = """Bank of America

Account Alert: Debit Card Used Online, by Phone or by Mail

Account: Debit card ending in 7625
Amount: $ 15.001
Made at: BUFFER PUBLISH PRO MO -+14152955970 ,CA
On: August 13, 2024

View details in Online Banking.
"""

# SCHEMA-DEBITCARD-USED — BATCHED; the Account/Amount/Made at/On block repeats.
USED_BATCH_BODY = """Bank of America

Account Alert: Debit Card Used Online, by Phone or by Mail

Account: Debit card ending in 7625
Amount: $ 15.00
Made at: BUFFER PUBLISH PRO MO -+14152955970 ,CA
On: August 13, 2024

Account: Debit card ending in 7625
Amount: $ 42.99
Made at: AMAZON MKTPL AMZN.COM -SEATTLE ,WA
On: August 13, 2024

Account: Debit card ending in 7625
Amount: $ 5.00
Made at: OPENAI CHATGPT -+14158799686 ,CA
On: August 12, 2024

Account: Debit card ending in 7625
Amount: $ 120.50
Made at: META ADS -MENLO PARK ,CA
On: August 12, 2024

Account: Debit card ending in 7625
Amount: $ 8.25
Made at: SPOTIFY USA -NEW YORK ,NY
On: August 11, 2024

View details in Online Banking.
"""

# SCHEMA-ACH-WITHDRAWAL — "$1,487.50" (comma thousands, no space after $), no card.
ACH_BODY = """Bank of America

A withdrawal was made over the limit you set

Amount $1,487.50
Type ELEC DRAFT (ACH)
Account Ad Buys 0353 - 0353
Merchant AUDACY PURCHASE
Transaction date June 13, 2025

View details in Online Banking.
"""

# SCHEMA-CURRENT-VARIANT — # REFINE (rendered fields only, no raw HTML sample).
CURRENT_BODY = """Bank of America

A transaction occurred over the limit you set

Amount $2,000.00
Debit/ATM card ending in 5723
Merchant SA EXPRESS NEWS ADV -SAN ANTONIO ,TX
Transaction type PURCH W/O PIN
Date July 07, 2026

View details in Online Banking.
"""

# A raw HTML-only body (BofA-style table) to exercise html_to_text/get_body_text.
OVERLIMIT_HTML = """<html><body>
<table>
<tr><td>Amount:</td><td>$&nbsp;856.00</td></tr>
<tr><td>Debit/ATM card:</td><td>ending in - 5723</td></tr>
<tr><td>Where:</td><td>at THE PHILADELPHIA INQUI-PHILADELPHIA ,PA</td></tr>
<tr><td>Transaction type:</td><td>PURCH W/O PIN</td></tr>
<tr><td>When:</td><td>on July 07, 2024</td></tr>
</table>
</body></html>"""

MSG_OVERLIMIT = "msgOVER111"
MSG_USED_SINGLE = "msgUSED1s"
MSG_USED_BATCH = "msgUSEDbat"
MSG_ACH = "msgACH333"
MSG_CURRENT = "msgCUR444"


# --- Subject routing -------------------------------------------------------


def test_subject_routing_table():
    assert schema_for_subject(SUBJ_OVERLIMIT) == SCHEMA_DEBITCARD_OVERLIMIT
    assert schema_for_subject(SUBJ_USED) == SCHEMA_DEBITCARD_USED
    assert schema_for_subject(SUBJ_ACH) == SCHEMA_ACH_WITHDRAWAL
    assert schema_for_subject(SUBJ_CURRENT) == SCHEMA_CURRENT_VARIANT
    # Best-effort routes fall through to the debit-card over-limit parser.
    assert schema_for_subject(SUBJ_ELEC) == SCHEMA_DEBITCARD_OVERLIMIT
    assert schema_for_subject(SUBJ_TRANSFER) == SCHEMA_DEBITCARD_OVERLIMIT
    # Case-insensitive.
    assert schema_for_subject(SUBJ_ACH.upper()) == SCHEMA_ACH_WITHDRAWAL
    # Unknown subject → no route.
    assert schema_for_subject("Your statement is ready") is None


# --- Amount cleaner (dual formats + footnote digit) ------------------------


def test_amount_cleaner_dual_formats_and_footnote():
    assert parse_amount("$ 856.00") == Decimal("856.00")       # space, no comma
    assert parse_amount("$1,487.50") == Decimal("1487.50")     # comma, no space
    assert parse_amount("$2,000.00") == Decimal("2000.00")
    assert parse_amount("$ 15.001") == Decimal("15.00")        # footnote "1" ignored
    assert parse_amount("$ 15.00") == Decimal("15.00")
    assert parse_amount("no dollar here") is None


def test_last4_extraction_variants():
    assert parse_last4("ending in - 5723") == "5723"
    assert parse_last4("Debit card ending in 7625") == "7625"
    assert parse_last4("Ad Buys 0353 - 0353") == "0353"       # trailing run
    assert parse_last4("ending in 5723") == "5723"


# --- SCHEMA-DEBITCARD-OVERLIMIT --------------------------------------------


def test_overlimit_field_extraction():
    txn = parse_single(
        OVERLIMIT_BODY, SCHEMA_DEBITCARD_OVERLIMIT, MSG_OVERLIMIT,
        fallback_date=date(2024, 7, 7),
    )
    assert txn is not None
    assert txn.amount == Decimal("-856.00")  # debit => negative (signed)
    assert txn.amount < 0
    assert txn.last4 == "5723"
    assert txn.merchant == "THE PHILADELPHIA INQUI-PHILADELPHIA ,PA"
    assert txn.txn_type == "PURCH W/O PIN"
    assert txn.posted_date == date(2024, 7, 7)
    assert txn.schema == SCHEMA_DEBITCARD_OVERLIMIT
    # Single alert => source_txn_id is the bare message id.
    assert txn.source_txn_id == MSG_OVERLIMIT
    assert txn.source_txn_id == email_scan_source_txn_id(MSG_OVERLIMIT)


def test_overlimit_missing_mandatory_fields_returns_none():
    txn = parse_single(
        "no money, no date here", SCHEMA_DEBITCARD_OVERLIMIT, MSG_OVERLIMIT,
        fallback_date=None,
    )
    assert txn is None


# --- SCHEMA-DEBITCARD-USED (single + batched) ------------------------------


def test_used_single_footnote_and_single_id():
    txns = parse_debitcard_used(
        USED_SINGLE_BODY, MSG_USED_SINGLE, fallback_date=date(2024, 8, 13)
    )
    assert len(txns) == 1
    t = txns[0]
    assert t.amount == Decimal("-15.00")  # footnote "1" ignored, not 15.001
    assert t.last4 == "7625"
    assert t.merchant == "BUFFER PUBLISH PRO MO -+14152955970 ,CA"
    assert t.posted_date == date(2024, 8, 13)
    # Single block => bare message id (no :i suffix).
    assert t.block_index is None
    assert t.source_txn_id == MSG_USED_SINGLE


def test_used_batched_yields_n_rows_with_distinct_ids():
    txns = parse_debitcard_used(
        USED_BATCH_BODY, MSG_USED_BATCH, fallback_date=date(2024, 8, 13)
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
    # Distinct message_id:i ids for each batched block.
    assert [t.source_txn_id for t in txns] == [
        f"{MSG_USED_BATCH}:0",
        f"{MSG_USED_BATCH}:1",
        f"{MSG_USED_BATCH}:2",
        f"{MSG_USED_BATCH}:3",
        f"{MSG_USED_BATCH}:4",
    ]
    assert len({t.source_txn_id for t in txns}) == 5


# --- SCHEMA-ACH-WITHDRAWAL -------------------------------------------------


def test_ach_withdrawal_extraction():
    txn = parse_single(
        ACH_BODY, SCHEMA_ACH_WITHDRAWAL, MSG_ACH, fallback_date=date(2025, 6, 13)
    )
    assert txn is not None
    assert txn.amount == Decimal("-1487.50")  # comma thousands handled
    assert txn.txn_type == "ELEC DRAFT (ACH)"
    assert txn.last4 == "0353"  # nickname + last4 "Ad Buys 0353 - 0353"
    assert txn.merchant == "AUDACY PURCHASE"
    assert txn.posted_date == date(2025, 6, 13)
    assert txn.schema == SCHEMA_ACH_WITHDRAWAL


# --- SCHEMA-CURRENT-VARIANT (# REFINE) -------------------------------------


def test_current_variant_extraction():
    txn = parse_single(
        CURRENT_BODY, SCHEMA_CURRENT_VARIANT, MSG_CURRENT,
        fallback_date=date(2026, 7, 7),
    )
    assert txn is not None
    assert txn.amount == Decimal("-2000.00")
    assert txn.last4 == "5723"
    assert txn.merchant == "SA EXPRESS NEWS ADV -SAN ANTONIO ,TX"
    assert txn.txn_type == "PURCH W/O PIN"
    assert txn.posted_date == date(2026, 7, 7)


# --- HTML-only body handling -----------------------------------------------


def test_html_to_text_strips_tags_and_entities():
    text = html_to_text(OVERLIMIT_HTML)
    assert "<td>" not in text
    assert "<table>" not in text
    assert "&nbsp;" not in text
    assert "Amount:" in text
    assert "856.00" in text


def test_get_body_text_renders_html_only_message():
    message = {
        "payload": {
            "mimeType": "text/html",
            "body": _b64(OVERLIMIT_HTML),
        }
    }
    text = get_body_text(message)
    assert "<td>" not in text
    # The rendered text is fully parseable by the over-limit schema.
    txn = parse_single(
        text, SCHEMA_DEBITCARD_OVERLIMIT, MSG_OVERLIMIT, fallback_date=None
    )
    assert txn is not None
    assert txn.amount == Decimal("-856.00")
    assert txn.last4 == "5723"
    assert txn.merchant == "THE PHILADELPHIA INQUI-PHILADELPHIA ,PA"
    assert txn.posted_date == date(2024, 7, 7)


# --- parse_message routing -------------------------------------------------


def test_parse_message_routes_by_subject():
    assert len(parse_message(OVERLIMIT_BODY, SUBJ_OVERLIMIT, MSG_OVERLIMIT, None)) == 1
    assert len(parse_message(USED_BATCH_BODY, SUBJ_USED, MSG_USED_BATCH, None)) == 5
    assert len(parse_message(ACH_BODY, SUBJ_ACH, MSG_ACH, None)) == 1
    assert parse_message("body", "Unknown subject", "m", None) == []


# --- Query building --------------------------------------------------------


def test_build_query_is_date_bounded():
    q = build_query(SUBJ_OVERLIMIT, lookback_days=2, today=date(2026, 7, 8))
    assert f'subject:"{SUBJ_OVERLIMIT}"' in q
    assert "after:2026/07/06" in q


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


# --- Insert idempotency ----------------------------------------------------


def test_insert_batched_inserts_then_skips():
    session = FakeSession()
    txns = parse_debitcard_used(USED_BATCH_BODY, MSG_USED_BATCH, date(2024, 8, 13))

    inserted, skipped = insert_transactions(session, txns)
    assert (inserted, skipped) == (5, 0)
    assert len(session.store) == 5

    # Re-inserting the SAME parsed transactions is a no-op (conflict-skip).
    inserted2, skipped2 = insert_transactions(session, txns)
    assert (inserted2, skipped2) == (0, 5)
    assert len(session.store) == 5


def test_same_message_parsed_twice_is_one_logical_row():
    session = FakeSession()
    first = parse_single(
        OVERLIMIT_BODY, SCHEMA_DEBITCARD_OVERLIMIT, MSG_OVERLIMIT, date(2024, 7, 7)
    )
    again = parse_single(
        OVERLIMIT_BODY, SCHEMA_DEBITCARD_OVERLIMIT, MSG_OVERLIMIT, date(2024, 7, 7)
    )

    insert_transactions(session, [first])
    insert_transactions(session, [again])
    assert len(session.store) == 1  # dedup on message-id-derived source_txn_id


# --- End-to-end run (faked Gmail) ------------------------------------------


def _b64(text: str) -> dict:
    import base64

    return {"data": base64.urlsafe_b64encode(text.encode()).decode()}


@pytest.fixture
def fake_gmail(monkeypatch):
    messages = {
        MSG_OVERLIMIT: {
            "id": MSG_OVERLIMIT, "internalDate": "1720310400000", "_body": OVERLIMIT_BODY,
        },
        MSG_USED_BATCH: {
            "id": MSG_USED_BATCH, "internalDate": "1723507200000", "_body": USED_BATCH_BODY,
        },
        MSG_ACH: {"id": MSG_ACH, "internalDate": "1749772800000", "_body": ACH_BODY},
        MSG_CURRENT: {
            "id": MSG_CURRENT, "internalDate": "1751846400000", "_body": CURRENT_BODY,
        },
    }
    # Keyed by the SUBJECT_ROUTES substrings (what build_query embeds), not the
    # full rendered subjects. The two best-effort routes return nothing.
    routing = {
        "Debit/ATM Card Transaction Over Your Chosen Alert Limit": MSG_OVERLIMIT,
        "Debit Card Used Online, by Phone or by Mail": MSG_USED_BATCH,
        "A withdrawal was made over the limit you set": MSG_ACH,
        "A transaction occurred over the limit you set": MSG_CURRENT,
    }

    def fake_search(service, query, max_results=2000):
        for subject, msg_id in routing.items():
            if f'subject:"{subject}"' in query:
                return iter([{"id": msg_id}])
        return iter([])

    def fake_get_message(service, message_id, fmt="full"):
        return messages[message_id]

    def fake_get_body_text(message):
        return message["_body"]

    from confirmed_ctl.gmail import client as gmail_client

    monkeypatch.setattr(gmail_client, "search_messages", fake_search)
    monkeypatch.setattr(gmail_client, "get_message", fake_get_message)
    monkeypatch.setattr(gmail_client, "get_body_text", fake_get_body_text)
    return object()  # a placeholder "service"


def test_scan_messages_collects_all_schemas(fake_gmail):
    txns = scan_messages(fake_gmail, lookback_days=7)
    # 1 over-limit + 5 batched used + 1 ACH + 1 current = 8
    assert len(txns) == 8
    by_schema = {}
    for t in txns:
        by_schema[t.schema] = by_schema.get(t.schema, 0) + 1
    assert by_schema[SCHEMA_DEBITCARD_OVERLIMIT] == 1
    assert by_schema[SCHEMA_DEBITCARD_USED] == 5
    assert by_schema[SCHEMA_ACH_WITHDRAWAL] == 1
    assert by_schema[SCHEMA_CURRENT_VARIANT] == 1


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
