"""Offline tests for the BofA email-scan ingestion adapter.

No live Gmail, no Postgres, no network. Gmail access is faked by monkeypatching
the ``confirmed_ctl.gmail.client`` module functions, and the DB is a tiny
in-memory ``FakeSession`` that emulates only the ORM primitives the adapter
uses (``query().filter_by().first()``, ``add``, ``begin_nested``, ``flush``,
``commit``) and enforces the ``(source, source_txn_id)`` uniqueness that the
real ``uq_bank_transactions_source_txn`` constraint provides.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from confirmed_ctl.ingest.dedup import email_scan_source_txn_id
from confirmed_ctl.ingest.email_scan import (
    SUBJECT_TYPE_A,
    SUBJECT_TYPE_B,
    build_query,
    insert_transactions,
    parse_type_a,
    parse_type_b,
    run_email_scan,
    scan_messages,
)

# --- Representative fixture email bodies (based on documented BofA formats) --

TYPE_A_BODY = """Bank of America

A transaction occurred over the limit you set.

Amount: $123.45
Where: SA EXPRESS NEWS ADV -SAN ANTONIO ,TX
Account: Debit card ending in 1234
Transaction type: PURCH W/O PIN
Date: July 07, 2026

View details in Online Banking.
"""

TYPE_B_BODY = """Bank of America

Your debit card was used for the following transactions:

$12.34 at SA EXPRESS NEWS ADV -SAN ANTONIO ,TX on 07/05/2026
$56.78 at AMAZON MKTPL AMZN.COM on 07/06/2026
$9.00 at STARBUCKS STORE 00123 on 07/06/2026

Debit card ending in 5678
"""

MSG_A = "msgAAA111"
MSG_B = "msgBBB222"


# --- Parser tests: Type A --------------------------------------------------


def test_type_a_field_extraction():
    txn = parse_type_a(TYPE_A_BODY, MSG_A, fallback_date=date(2026, 7, 7))
    assert txn is not None
    assert txn.amount == Decimal("-123.45")  # debit => negative (signed)
    assert txn.amount < 0
    assert "SA EXPRESS NEWS ADV" in txn.merchant
    assert txn.last4 == "1234"
    assert txn.txn_type == "PURCH W/O PIN"
    assert txn.posted_date == date(2026, 7, 7)
    assert txn.mission_type == "A"


def test_type_a_source_txn_id_is_message_id():
    txn = parse_type_a(TYPE_A_BODY, MSG_A, fallback_date=date(2026, 7, 7))
    assert txn.source_txn_id == MSG_A
    assert txn.source_txn_id == email_scan_source_txn_id(MSG_A)


def test_type_a_missing_mandatory_fields_returns_none():
    assert parse_type_a("no money, no date here", MSG_A, fallback_date=None) is None


# --- Parser tests: Type B (batched) ----------------------------------------


def test_type_b_parses_multiple_line_items():
    txns = parse_type_b(TYPE_B_BODY, MSG_B, fallback_date=date(2026, 7, 6))
    assert len(txns) == 3
    assert [t.amount for t in txns] == [
        Decimal("-12.34"),
        Decimal("-56.78"),
        Decimal("-9.00"),
    ]
    assert "SA EXPRESS NEWS" in txns[0].merchant
    assert "AMAZON" in txns[1].merchant
    assert txns[0].posted_date == date(2026, 7, 5)


def test_type_b_source_txn_id_has_line_index():
    txns = parse_type_b(TYPE_B_BODY, MSG_B, fallback_date=date(2026, 7, 6))
    assert [t.source_txn_id for t in txns] == [
        f"{MSG_B}:0",
        f"{MSG_B}:1",
        f"{MSG_B}:2",
    ]
    # distinct line items of one email get distinct ids
    assert len({t.source_txn_id for t in txns}) == 3


# --- Query building --------------------------------------------------------


def test_build_query_is_date_bounded():
    q = build_query(SUBJECT_TYPE_A, lookback_days=2, today=date(2026, 7, 8))
    assert f'subject:"{SUBJECT_TYPE_A}"' in q
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


def test_insert_transactions_inserts_then_skips():
    session = FakeSession()
    txns = parse_type_b(TYPE_B_BODY, MSG_B, fallback_date=date(2026, 7, 6))

    inserted, skipped = insert_transactions(session, txns)
    assert (inserted, skipped) == (3, 0)
    assert len(session.store) == 3

    # Re-inserting the SAME parsed transactions is a no-op (conflict-skip).
    inserted2, skipped2 = insert_transactions(session, txns)
    assert (inserted2, skipped2) == (0, 3)
    assert len(session.store) == 3


def test_same_message_parsed_twice_is_one_logical_row():
    session = FakeSession()
    first = parse_type_a(TYPE_A_BODY, MSG_A, fallback_date=date(2026, 7, 7))
    again = parse_type_a(TYPE_A_BODY, MSG_A, fallback_date=date(2026, 7, 7))

    insert_transactions(session, [first])
    insert_transactions(session, [again])
    assert len(session.store) == 1  # dedup on message-id-derived source_txn_id


# --- End-to-end run (faked Gmail) ------------------------------------------


@pytest.fixture
def fake_gmail(monkeypatch):
    messages = {
        MSG_A: {"id": MSG_A, "internalDate": "1751846400000", "_body": TYPE_A_BODY},
        MSG_B: {"id": MSG_B, "internalDate": "1751760000000", "_body": TYPE_B_BODY},
    }

    def fake_search(service, query, max_results=2000):
        if SUBJECT_TYPE_A in query:
            return iter([{"id": MSG_A}])
        if SUBJECT_TYPE_B in query:
            return iter([{"id": MSG_B}])
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


def test_scan_messages_collects_both_missions(fake_gmail):
    txns = scan_messages(fake_gmail, lookback_days=7)
    # 1 Type A + 3 Type B line items
    assert len(txns) == 4
    assert sum(1 for t in txns if t.mission_type == "A") == 1
    assert sum(1 for t in txns if t.mission_type == "B") == 3


def test_run_email_scan_is_idempotent(fake_gmail):
    session = FakeSession()

    result = run_email_scan(session, lookback_days=7, service=fake_gmail)
    assert result["found"] == 4
    assert result["inserted"] == 4
    assert result["skipped"] == 0
    assert len(session.sync_logs) == 1
    assert session.sync_logs[0].source == "email-scan"

    # Second pass over the same emails inserts nothing (idempotent).
    result2 = run_email_scan(session, lookback_days=7, service=fake_gmail)
    assert result2["found"] == 4
    assert result2["inserted"] == 0
    assert result2["skipped"] == 4
    assert len(session.store) == 4
    assert len(session.sync_logs) == 2
