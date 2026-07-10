"""Endpoint tests for /candidates and /unconfirmed.

The CRM adapter and the Postgres session are mocked — no live CRM, no live DB.
We exercise the real Flask routing/JSON via a test client and assert the
configured/unconfigured/not-found branches and the unconfirmed set-subtraction.
"""

import json
from contextlib import contextmanager
from datetime import date, datetime, timezone

import pytest

flask = pytest.importorskip("flask")

from confirmed_ctl.api import routes  # noqa: E402
from confirmed_ctl.db.models import (  # noqa: E402
    AdConfirmation,
    BankTransaction,
    CrmAd,
)


@pytest.fixture
def client():
    app = flask.Flask(__name__)
    app.register_blueprint(routes.confirmed_ctl_bp)
    return app.test_client()


def _fake_txn():
    return BankTransaction(
        id=1,
        source="email-scan",
        source_txn_id="abc",
        txn_date=date(2026, 6, 17),
        total_amount=1368.0,
        vendor_name="MIAMI HERALD ACH",
        account_name="BofA Checking",
        payment_ref_num="REF1",
        private_note="memo",
    )


def _configure_crm(monkeypatch, *, configured=True):
    monkeypatch.setattr(routes.crm_client, "is_configured", lambda: configured)


def _patch_db(monkeypatch, session):
    @contextmanager
    def fake_get_db():
        yield session

    monkeypatch.setattr(routes, "get_db", fake_get_db)


# --------------------------------------------------------------------------- #
# /candidates
# --------------------------------------------------------------------------- #
def test_candidates_503_when_crm_unconfigured(client, monkeypatch):
    _configure_crm(monkeypatch, configured=False)
    resp = client.get("/confirmed-ctl/candidates/REC123")
    assert resp.status_code == 503
    assert resp.get_json()["status"] == "crm_not_configured"


def test_candidates_404_when_ad_not_found(client, monkeypatch):
    _configure_crm(monkeypatch)
    monkeypatch.setattr(routes.crm_client, "get_ad", lambda _id: None)
    resp = client.get("/confirmed-ctl/candidates/REC123")
    assert resp.status_code == 404
    assert resp.get_json()["status"] == "not_found"


def test_candidates_returns_ranked_txns(client, monkeypatch):
    _configure_crm(monkeypatch)
    ad = CrmAd(
        crm_id="REC123",
        ad_number="IPR00160880",
        client_name="Eduexplora International",
        newspaper_name="Miami Herald",
        run_date=date(2026, 6, 15),
        expected_charge_date=date(2026, 6, 17),
        expected_amount=1368.0,
        case_number="A-2026-0042",
        state="CA",
        attorney="Jane Atty",
        entity="JKT",
        job_title="Analyst",
        run_end=date(2026, 6, 20),
        status_news='["Active"]',
        owner="karl",
        approved_date=date(2026, 6, 1),
        buy_date=date(2026, 6, 17),
        beneficiary_first="Ann",
        beneficiary_last="Doe",
        clearance_status='["Confirmed"]',
    )
    monkeypatch.setattr(routes.crm_client, "get_ad", lambda _id: ad)
    monkeypatch.setattr(
        routes, "get_candidate_transactions",
        lambda db, ad: [{"transaction": _fake_txn(), "score": 0.91}],
    )
    thread = {
        "thread_id": "t1",
        "subject": "Ad IPR00160880 receipt",
        "from": "billing@miamiherald.com",
        "date": "Tue, 16 Jun 2026 10:00:00 -0400",
        "snippet": "Your ad IPR00160880 ran",
        "message_count": 2,
        "gmail_url": (
            "https://mail.google.com/mail/?authuser=karl@perm-ads.com#all/t1"
        ),
        "matched_by": "ad_number",
    }
    captured = {}

    def _fake_search(ad_number, newspaper_name=None, charge_date=None, max_results=8):
        captured.update(
            ad_number=ad_number,
            newspaper_name=newspaper_name,
            charge_date=charge_date,
        )
        return [thread]

    monkeypatch.setattr(routes, "search_threads_by_ad_number", _fake_search)
    _patch_db(monkeypatch, object())

    resp = client.get("/confirmed-ctl/candidates/REC123")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ad"]["crm_id"] == "REC123"
    assert data["ad"]["ad_number"] == "IPR00160880"
    # Richer ad-identifying fields (ABCF-X columns) exposed on the candidates ad.
    assert data["ad"]["case_number"] == "A-2026-0042"
    assert data["ad"]["state"] == "CA"
    assert data["ad"]["attorney"] == "Jane Atty"
    assert data["ad"]["entity"] == "JKT"
    # Additional ABCF-X reconcile columns exposed on the candidates ad.
    assert data["ad"]["job_title"] == "Analyst"
    assert data["ad"]["run_end"] == "2026-06-20"
    assert data["ad"]["status_news"] == '["Active"]'
    assert data["ad"]["owner"] == "karl"
    # New ABCF-X contract columns exposed on the candidates ad.
    assert data["ad"]["approved_date"] == "2026-06-01"
    assert data["ad"]["buy_date"] == "2026-06-17"
    assert data["ad"]["beneficiary_first"] == "Ann"
    assert data["ad"]["beneficiary_last"] == "Doe"
    assert data["ad"]["clearance_status"] == '["Confirmed"]'
    # The excluded near-miss array is always present (empty here).
    assert data["excluded"] == []
    assert len(data["bank_candidates"]) == 1
    cand = data["bank_candidates"][0]
    assert cand["txn_id"] == 1
    assert cand["amount"] == 1368.0
    assert cand["score"] == 0.91
    # Newspaper name + expected charge date are forwarded into the search.
    assert captured["newspaper_name"] == "Miami Herald"
    assert captured["charge_date"] == date(2026, 6, 17)
    # Each surfaced thread carries gmail_url + matched_by out to the JSON.
    assert data["gmail_threads"] == [thread]
    assert data["gmail_threads"][0]["gmail_url"].startswith(
        "https://mail.google.com/mail/?authuser="
    )
    assert data["gmail_threads"][0]["matched_by"] == "ad_number"
    assert data["gmail_error"] is None
    assert data["gmail_note"] is None


def test_candidates_502_when_crm_errors(client, monkeypatch):
    # CRM configured but the adapter raises (outage / bad creds / allowlist not
    # granted). Must be a controlled 502, NOT an unhandled 500.
    _configure_crm(monkeypatch)

    def _boom(_id):
        raise RuntimeError("pymysql: (2003) Can't connect to MySQL server")

    monkeypatch.setattr(routes.crm_client, "get_ad", _boom)
    resp = client.get("/confirmed-ctl/candidates/REC123")
    assert resp.status_code == 502
    body = resp.get_json()
    assert body["status"] == "crm_unavailable"
    # No stack trace / raw exception text leaked to the client.
    assert "pymysql" not in body["detail"]
    assert "2003" not in body["detail"]


def test_candidates_serialization_none_and_zero_safe(client, monkeypatch):
    # expected_amount == 0.0 must serialize as 0.0 (not None); run_date == None
    # must serialize as None (not the string "None").
    _configure_crm(monkeypatch)
    ad = CrmAd(
        crm_id="REC0",
        ad_number="AD-0",
        newspaper_name="Miami Herald",
        run_date=None,
        expected_charge_date=date(2026, 6, 17),
        expected_amount=0.0,
    )
    monkeypatch.setattr(routes.crm_client, "get_ad", lambda _id: ad)
    monkeypatch.setattr(routes, "get_candidate_transactions", lambda db, ad: [])
    monkeypatch.setattr(
        routes, "search_threads_by_ad_number", lambda *a, **k: []
    )
    _patch_db(monkeypatch, object())

    resp = client.get("/confirmed-ctl/candidates/REC0")
    assert resp.status_code == 200
    ad_json = resp.get_json()["ad"]
    assert ad_json["expected_amount"] == 0.0
    assert ad_json["run_date"] is None
    # Unset richer fields serialize as null (not the string "None").
    assert ad_json["case_number"] is None
    assert ad_json["state"] is None
    assert ad_json["attorney"] is None
    assert ad_json["entity"] is None
    # Additional ABCF-X columns also serialize as null when unset.
    assert ad_json["job_title"] is None
    assert ad_json["run_end"] is None
    assert ad_json["status_news"] is None
    assert ad_json["owner"] is None
    # New ABCF-X contract columns also serialize as null when unset.
    assert ad_json["approved_date"] is None
    assert ad_json["buy_date"] is None
    assert ad_json["beneficiary_first"] is None
    assert ad_json["beneficiary_last"] is None
    assert ad_json["clearance_status"] is None


def test_candidates_surfaces_gmail_error_on_failure(client, monkeypatch):
    # A real Gmail search failure must NOT be silently swallowed: the popup
    # still returns 200, but gmail_error is surfaced (not pretend-empty).
    _configure_crm(monkeypatch)
    ad = CrmAd(crm_id="REC123", ad_number="IPR1", newspaper_name="Miami Herald",
               run_date=date(2026, 6, 15), expected_charge_date=date(2026, 6, 17),
               expected_amount=100.0)
    monkeypatch.setattr(routes.crm_client, "get_ad", lambda _id: ad)
    monkeypatch.setattr(routes, "get_candidate_transactions", lambda db, ad: [])

    def _boom(*a, **k):
        raise RuntimeError("gmail down")

    monkeypatch.setattr(routes, "search_threads_by_ad_number", _boom)
    _patch_db(monkeypatch, object())

    resp = client.get("/confirmed-ctl/candidates/REC123")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["gmail_threads"] == []
    assert isinstance(data["gmail_error"], str) and data["gmail_error"]
    assert data["gmail_note"] is None
    # No raw exception text leaked to the client.
    assert "gmail down" not in data["gmail_error"]


def test_candidates_blank_ad_number_sets_note_without_search(client, monkeypatch):
    # Blank/whitespace ad number => gmail_note, gmail_threads=[], and the Gmail
    # client is NEVER called (guarded before search).
    _configure_crm(monkeypatch)
    ad = CrmAd(crm_id="REC123", ad_number="   ", newspaper_name="Miami Herald",
               run_date=date(2026, 6, 15), expected_charge_date=date(2026, 6, 17),
               expected_amount=100.0)
    monkeypatch.setattr(routes.crm_client, "get_ad", lambda _id: ad)
    monkeypatch.setattr(routes, "get_candidate_transactions", lambda db, ad: [])

    def _must_not_search(*a, **k):  # pragma: no cover
        raise AssertionError("search must not run for a blank ad number")

    monkeypatch.setattr(routes, "search_threads_by_ad_number", _must_not_search)
    _patch_db(monkeypatch, object())

    resp = client.get("/confirmed-ctl/candidates/REC123")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["gmail_threads"] == []
    assert data["gmail_note"] == "No ad number on record"
    assert data["gmail_error"] is None


# --------------------------------------------------------------------------- #
# /unconfirmed
# --------------------------------------------------------------------------- #
def test_unconfirmed_503_when_crm_unconfigured(client, monkeypatch):
    _configure_crm(monkeypatch, configured=False)
    resp = client.get("/confirmed-ctl/unconfirmed")
    assert resp.status_code == 503
    assert resp.get_json()["status"] == "crm_not_configured"


def test_unconfirmed_502_when_crm_errors(client, monkeypatch):
    # CRM configured but list_clearances raises -> controlled 502, not 500.
    _configure_crm(monkeypatch)

    def _boom():
        raise RuntimeError("pymysql: (1045) Access denied for user")

    monkeypatch.setattr(routes.crm_client, "list_clearances", _boom)
    resp = client.get("/confirmed-ctl/unconfirmed")
    assert resp.status_code == 502
    body = resp.get_json()
    assert body["status"] == "crm_unavailable"
    assert "pymysql" not in body["detail"]
    assert "1045" not in body["detail"]


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """Minimal session: query(AdConfirmation.ad_crm_id).all() -> row tuples."""

    def __init__(self, confirmed_ids):
        self._rows = [(cid,) for cid in confirmed_ids]

    def query(self, *args, **kwargs):
        return _FakeQuery(self._rows)


def test_unconfirmed_excludes_already_confirmed(client, monkeypatch):
    _configure_crm(monkeypatch)
    clearances = [
        CrmAd(crm_id="A", ad_number="AD-A", newspaper_name="Miami Herald",
              run_date=date(2026, 6, 1), expected_charge_date=date(2026, 6, 2),
              expected_amount=100.0),
        CrmAd(crm_id="B", ad_number="AD-B", newspaper_name="Sun Sentinel",
              run_date=date(2026, 6, 3), expected_charge_date=date(2026, 6, 4),
              expected_amount=200.0, case_number="B-2026-0007", state="NY",
              attorney="John Atty", entity="PA", job_title="Engineer",
              run_end=date(2026, 6, 10), status_news='["Active"]', owner="karl",
              approved_date=date(2026, 6, 1), buy_date=date(2026, 6, 4),
              beneficiary_first="Bob", beneficiary_last="Smith",
              clearance_status='["Confirmed"]'),
    ]
    monkeypatch.setattr(routes.crm_client, "list_clearances", lambda: clearances)
    # "A" is already confirmed in Postgres -> only "B" should remain.
    _patch_db(monkeypatch, _FakeSession(confirmed_ids=["A"]))

    resp = client.get("/confirmed-ctl/unconfirmed")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["count"] == 1
    assert [a["crm_id"] for a in data["ads"]] == ["B"]
    assert data["ads"][0]["expected_amount"] == 200.0
    # Richer ad-identifying fields (ABCF-X columns) exposed on the unconfirmed ads.
    ad_b = data["ads"][0]
    assert ad_b["case_number"] == "B-2026-0007"
    assert ad_b["state"] == "NY"
    assert ad_b["attorney"] == "John Atty"
    assert ad_b["entity"] == "PA"
    # Additional ABCF-X reconcile columns exposed on the unconfirmed ads.
    assert ad_b["job_title"] == "Engineer"
    assert ad_b["run_end"] == "2026-06-10"
    assert ad_b["status_news"] == '["Active"]'
    assert ad_b["owner"] == "karl"
    # New ABCF-X contract columns exposed on the unconfirmed ads.
    assert ad_b["approved_date"] == "2026-06-01"
    assert ad_b["buy_date"] == "2026-06-04"
    assert ad_b["beneficiary_first"] == "Bob"
    assert ad_b["beneficiary_last"] == "Smith"
    assert ad_b["clearance_status"] == '["Confirmed"]'


def test_unconfirmed_all_when_none_confirmed(client, monkeypatch):
    _configure_crm(monkeypatch)
    clearances = [
        CrmAd(crm_id="A", ad_number="AD-A", expected_amount=100.0),
        CrmAd(crm_id="B", ad_number="AD-B", expected_amount=200.0),
    ]
    monkeypatch.setattr(routes.crm_client, "list_clearances", lambda: clearances)
    _patch_db(monkeypatch, _FakeSession(confirmed_ids=[]))

    resp = client.get("/confirmed-ctl/unconfirmed")
    data = resp.get_json()
    assert data["count"] == 2
    assert {a["crm_id"] for a in data["ads"]} == {"A", "B"}


# --------------------------------------------------------------------------- #
# /candidates — excluded near-miss array flows through
# --------------------------------------------------------------------------- #
def test_candidates_excluded_array_surfaced(client, monkeypatch):
    _configure_crm(monkeypatch)
    ad = CrmAd(crm_id="REC123", ad_number="IPR1", newspaper_name="Miami Herald",
               run_date=date(2026, 6, 15), expected_charge_date=date(2026, 6, 17),
               expected_amount=2000.0)
    monkeypatch.setattr(routes.crm_client, "get_ad", lambda _id: ad)
    monkeypatch.setattr(routes, "get_candidate_transactions", lambda db, ad: [])
    monkeypatch.setattr(routes, "search_threads_by_ad_number", lambda *a, **k: [])
    excluded = [
        {"txn_id": 9, "source": "email-scan", "source_txn_id": "x9",
         "txn_date": "2026-07-15", "amount": -2000.0, "vendor_name": "LA TIMES",
         "reason": "out_of_window"},
    ]
    monkeypatch.setattr(routes, "get_excluded_transactions", lambda db, ad: excluded)
    _patch_db(monkeypatch, object())

    resp = client.get("/confirmed-ctl/candidates/REC123")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["excluded"] == excluded
    assert data["excluded"][0]["reason"] == "out_of_window"
    assert data["excluded"][0]["txn_date"] == "2026-07-15"


# --------------------------------------------------------------------------- #
# /reconciled
# --------------------------------------------------------------------------- #
class _ReconciledQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._rows)


class _ReconciledSession:
    """query(AdConfirmation) -> confs; query(BankTransaction) -> txns."""

    def __init__(self, confs, txns):
        self._confs = confs
        self._txns = txns

    def query(self, model, *args, **kwargs):
        if model is AdConfirmation:
            return _ReconciledQuery(self._confs)
        if model is BankTransaction:
            return _ReconciledQuery(self._txns)
        return _ReconciledQuery([])


def test_reconciled_503_when_crm_unconfigured(client, monkeypatch):
    _configure_crm(monkeypatch, configured=False)
    resp = client.get("/confirmed-ctl/reconciled")
    assert resp.status_code == 503
    assert resp.get_json()["status"] == "crm_not_configured"


def test_reconciled_502_when_crm_errors(client, monkeypatch):
    _configure_crm(monkeypatch)

    def _boom():
        raise RuntimeError("pymysql: (2003) Can't connect")

    monkeypatch.setattr(routes.crm_client, "list_reconciled", _boom)
    resp = client.get("/confirmed-ctl/reconciled")
    assert resp.status_code == 502
    body = resp.get_json()
    assert body["status"] == "crm_unavailable"
    assert "pymysql" not in body["detail"]


def test_reconciled_shape_ordering_and_only_reconciled(client, monkeypatch):
    _configure_crm(monkeypatch)
    # Three Done ads from the CRM; only A and B were reconciled by this tool
    # (have an ad_confirmations row). C must be excluded.
    reconciled_ads = [
        CrmAd(crm_id="A", ad_number="AD-A", newspaper_name="Miami Herald",
              run_date=date(2026, 6, 1), expected_charge_date=date(2026, 6, 2),
              expected_amount=100.0, clearance_status='["Done"]',
              approved_date=date(2026, 5, 30), buy_date=date(2026, 6, 2),
              beneficiary_first="John", beneficiary_last="Doe"),
        CrmAd(crm_id="B", ad_number="AD-B", newspaper_name="Sun Sentinel",
              run_date=date(2026, 6, 3), expected_charge_date=date(2026, 6, 4),
              expected_amount=200.0, clearance_status='["Done"]'),
        CrmAd(crm_id="C", ad_number="AD-C", expected_amount=300.0,
              clearance_status='["Done"]'),
    ]
    monkeypatch.setattr(routes.crm_client, "list_reconciled", lambda: reconciled_ads)

    # A confirmed earlier than B -> B must come first (confirmed_at DESC).
    conf_a = AdConfirmation(
        ad_crm_id="A", ad_number="AD-A", bank_txn_id=11,
        gmail_thread_id="thrA",
        confirmed_at=datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc),
    )
    conf_b = AdConfirmation(
        ad_crm_id="B", ad_number="AD-B", bank_txn_id=12,
        gmail_thread_id="thrB",
        confirmed_at=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc),
        receipt_file_path="/var/lib/confirmed-ctl/receipts/2026/06/AD-B/receipt.pdf",
    )
    txn_a = BankTransaction(id=11, source="email-scan", source_txn_id="a",
                            txn_date=date(2026, 6, 2), total_amount=-100.0,
                            bofa_gmail_thread_id="bofaA")
    txn_b = BankTransaction(id=12, source="email-scan", source_txn_id="b",
                            txn_date=date(2026, 6, 4), total_amount=-200.0)
    _patch_db(monkeypatch, _ReconciledSession([conf_a, conf_b], [txn_a, txn_b]))

    resp = client.get("/confirmed-ctl/reconciled")
    assert resp.status_code == 200
    data = resp.get_json()
    # Only A and B (C has no confirmation row) and ordered by confirmed_at DESC.
    assert data["count"] == 2
    assert [a["crm_id"] for a in data["ads"]] == ["B", "A"]

    ad_a = next(a for a in data["ads"] if a["crm_id"] == "A")
    # CrmAd contract fields (incl. the new ones) are present.
    assert ad_a["ad_number"] == "AD-A"
    assert ad_a["clearance_status"] == '["Done"]'
    assert ad_a["approved_date"] == "2026-05-30"
    assert ad_a["buy_date"] == "2026-06-02"
    assert ad_a["beneficiary_first"] == "John"
    assert ad_a["beneficiary_last"] == "Doe"
    # Mapped bank + gmail + confirmed_at info.
    assert ad_a["bank_txn_id"] == 11
    assert ad_a["bank_amount"] == -100.0
    assert ad_a["bank_txn_date"] == "2026-06-02"
    assert ad_a["gmail_thread_id"] == "thrA"
    assert ad_a["gmail_url"].startswith("https://mail.google.com/mail/?authuser=")
    assert ad_a["gmail_url"].endswith("#all/thrA")
    # BofA-alert deep link for the mapped bank txn (from bofa_gmail_thread_id).
    assert ad_a["bofa_gmail_url"].endswith("#all/bofaA")
    assert ad_a["confirmed_at"].startswith("2026-06-05")
    # Receipt integration: A has no receipt, B has one.
    assert ad_a["has_receipt"] is False
    assert ad_a["receipt_file_path"] is None
    ad_b = next(a for a in data["ads"] if a["crm_id"] == "B")
    assert ad_b["has_receipt"] is True
    assert ad_b["receipt_file_path"].endswith("AD-B/receipt.pdf")


def test_reconciled_none_safe_when_no_bank_txn(client, monkeypatch):
    _configure_crm(monkeypatch)
    reconciled_ads = [
        CrmAd(crm_id="A", ad_number="AD-A", expected_amount=100.0,
              clearance_status='["Done"]'),
    ]
    monkeypatch.setattr(routes.crm_client, "list_reconciled", lambda: reconciled_ads)
    # Confirmation with no bank_txn_id and no gmail thread -> None-safe fields.
    conf_a = AdConfirmation(ad_crm_id="A", ad_number="AD-A", bank_txn_id=None,
                            gmail_thread_id=None, confirmed_at=None)
    _patch_db(monkeypatch, _ReconciledSession([conf_a], []))

    resp = client.get("/confirmed-ctl/reconciled")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["count"] == 1
    ad = data["ads"][0]
    assert ad["bank_txn_id"] is None
    assert ad["bank_amount"] is None
    assert ad["bank_txn_date"] is None
    assert ad["gmail_thread_id"] is None
    assert ad["gmail_url"] == ""
    # No mapped bank txn -> BofA deep link is the empty string (None-safe).
    assert ad["bofa_gmail_url"] == ""
    assert ad["confirmed_at"] is None


# --------------------------------------------------------------------------- #
# /bank-transaction/<txn_id> — read-only Bank-Trx modal detail
# --------------------------------------------------------------------------- #
class _GetConfQuery:
    """Fake ``query(AdConfirmation).filter(...).first()`` chain for the modal.

    Returns the seeded ad-confirmation row (or ``None``) so the widened
    Related-CRM block can surface the ad-confirm Gmail thread.
    """

    def __init__(self, ad_conf):
        self._ad_conf = ad_conf

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._ad_conf


class _GetSession:
    """Minimal session exposing ``get(BankTransaction, pk)`` -> a fixed row and
    ``query(AdConfirmation)`` -> the seeded ad-confirmation row.

    Mirrors ``Session.get``: returns the stored txn (representing the row for the
    requested id) or ``None`` to model an unknown id (404 path). ``query`` serves
    the ad-confirm Gmail lookup added to the Related-CRM block (None-safe).
    """

    def __init__(self, txn, ad_conf=None):
        self._txn = txn
        self._ad_conf = ad_conf

    def get(self, model, pk):
        return self._txn

    def query(self, *args, **kwargs):
        return _GetConfQuery(self._ad_conf)


def _detail_txn(**overrides):
    """A BankTransaction seed for the detail endpoint tests."""
    fields = dict(
        id=42,
        source="email-scan",
        source_txn_id="msg-123:0",
        txn_date=date(2026, 6, 26),
        total_amount=-2226.94,
        vendor_name="LA TIMES MEDIA GR",
        line_descriptions=["col-fallback-line"],
        raw_json={
            "merchant_raw": "CHECKCARD LA TIMES MEDIA GR EL SEGUNDO CA ON 06/26 Debit",
            "merchant": "LA TIMES MEDIA GR",
            "posted_date": "2026-06-27",
        },
        confirmed_ad_crm_id=None,
        confirmed_at=None,
        ignored=False,
        ignore_reason=None,
        created_in_db=datetime(2026, 6, 28, 9, 30, tzinfo=timezone.utc),
    )
    fields.update(overrides)
    return BankTransaction(**fields)


def test_bank_transaction_404_unknown_id(client, monkeypatch):
    _patch_db(monkeypatch, _GetSession(None))
    resp = client.get("/confirmed-ctl/bank-transaction/999999")
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "not_found"}


def test_bank_transaction_consumed_related_populated(client, monkeypatch):
    # A CONSUMED txn (confirmed_ad_crm_id set) -> related CRM summary populated.
    txn = _detail_txn(confirmed_ad_crm_id="REC777",
                      confirmed_at=datetime(2026, 6, 29, 10, 0, tzinfo=timezone.utc),
                      bofa_gmail_thread_id="bofaThr1")
    ad_conf = AdConfirmation(ad_crm_id="REC777", gmail_thread_id="adcThr1")
    _patch_db(monkeypatch, _GetSession(txn, ad_conf=ad_conf))
    ad = CrmAd(crm_id="REC777", ad_number="IPR00160880",
               client_name="Eduexplora International", newspaper_name="LA Times",
               case_number="A-2026-0042", job_title="Software Engineer",
               beneficiary_first="Jane", beneficiary_last="Doe",
               attorney="Jane Atty", run_date=date(2026, 6, 15),
               run_end=date(2026, 6, 20))
    monkeypatch.setattr(routes.crm_client, "get_ad", lambda _id: ad)

    resp = client.get("/confirmed-ctl/bank-transaction/42")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["txn_id"] == 42
    # amount is the raw signed total_amount (debit stays negative, as-is).
    assert data["amount"] == -2226.94
    assert data["vendor_name"] == "LA TIMES MEDIA GR"
    # merchant_raw / merchant / posted_date come from raw_json (a dict here).
    assert data["merchant_raw"].startswith("CHECKCARD LA TIMES MEDIA GR")
    assert data["merchant"] == "LA TIMES MEDIA GR"
    assert data["posted_date"] == "2026-06-27"
    assert data["txn_date"] == "2026-06-26"
    assert data["source"] == "email-scan"
    assert data["source_txn_id"] == "msg-123:0"
    assert data["ignored"] is False
    assert data["ignore_reason"] is None
    assert data["confirmed_ad_crm_id"] == "REC777"
    assert data["confirmed_at"].startswith("2026-06-29")
    assert data["created_at"].startswith("2026-06-28")
    # Top-level BofA-alert Gmail deep link (distinct from the ad-confirm thread).
    assert data["bofa_gmail_thread_id"] == "bofaThr1"
    assert data["bofa_gmail_url"].startswith(
        "https://mail.google.com/mail/?authuser="
    )
    assert data["bofa_gmail_url"].endswith("#all/bofaThr1")
    # Related CRM summary populated for the consumed txn (widened contract).
    assert "related_error" not in data
    rel = data["related"]
    # Existing five keys preserved.
    assert rel["crm_id"] == "REC777"
    assert rel["case_number"] == "A-2026-0042"
    assert rel["client_name"] == "Eduexplora International"
    assert rel["ad_number"] == "IPR00160880"
    assert rel["newspaper_name"] == "LA Times"
    # Widened Related-CRM fields.
    assert rel["job_title"] == "Software Engineer"
    assert rel["beneficiary_first"] == "Jane"
    assert rel["beneficiary_last"] == "Doe"
    assert rel["attorney"] == "Jane Atty"
    assert rel["run_date"] == "2026-06-15"
    assert rel["run_end"] == "2026-06-20"
    # Ad-confirmation Gmail thread + deep link (from ad_confirmations, NOT BofA).
    assert rel["ad_confirm_gmail_thread_id"] == "adcThr1"
    assert rel["ad_confirm_gmail_url"].endswith("#all/adcThr1")
    # The two Gmail links are genuinely different threads.
    assert data["bofa_gmail_url"] != rel["ad_confirm_gmail_url"]


def test_bank_transaction_unconsumed_related_null(client, monkeypatch):
    # An UNCONSUMED txn (no confirmed_ad_crm_id) -> related is null; get_ad is
    # never even called.
    txn = _detail_txn(confirmed_ad_crm_id=None)
    _patch_db(monkeypatch, _GetSession(txn))

    def _must_not_lookup(_id):  # pragma: no cover
        raise AssertionError("get_ad must not run for an unconsumed txn")

    monkeypatch.setattr(routes.crm_client, "get_ad", _must_not_lookup)

    resp = client.get("/confirmed-ctl/bank-transaction/42")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["confirmed_ad_crm_id"] is None
    assert data["related"] is None
    assert "related_error" not in data
    # No BofA thread captured for this seed -> thread id null, url empty string.
    assert data["bofa_gmail_thread_id"] is None
    assert data["bofa_gmail_url"] == ""


def test_bank_transaction_raw_json_dict_extraction(client, monkeypatch):
    # raw_json stored as a dict (JSONB) -> merchant/posted_date extracted; the
    # raw_json line_descriptions wins over the column fallback.
    txn = _detail_txn(raw_json={
        "merchant_raw": "RAW MEMO STRING",
        "merchant": "Merchant Co",
        "posted_date": "2026-07-01",
        "line_descriptions": ["from-raw-json"],
    })
    _patch_db(monkeypatch, _GetSession(txn))

    resp = client.get("/confirmed-ctl/bank-transaction/42")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["merchant_raw"] == "RAW MEMO STRING"
    assert data["merchant"] == "Merchant Co"
    assert data["posted_date"] == "2026-07-01"
    assert data["line_descriptions"] == ["from-raw-json"]


def test_bank_transaction_raw_json_string_extraction(client, monkeypatch):
    # raw_json stored as a JSON STRING must be parsed; missing line_descriptions
    # in raw_json falls back to the ARRAY column value.
    raw_str = (
        '{"merchant_raw": "STRING MEMO", "merchant": "Stringy Inc", '
        '"posted_date": "2026-07-02"}'
    )
    txn = _detail_txn(raw_json=raw_str, line_descriptions=["col-fallback-line"])
    _patch_db(monkeypatch, _GetSession(txn))

    resp = client.get("/confirmed-ctl/bank-transaction/42")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["merchant_raw"] == "STRING MEMO"
    assert data["merchant"] == "Stringy Inc"
    assert data["posted_date"] == "2026-07-02"
    # raw_json had no line_descriptions -> column fallback used.
    assert data["line_descriptions"] == ["col-fallback-line"]


def test_bank_transaction_related_error_when_crm_fails(client, monkeypatch):
    # The OPTIONAL related-CRM lookup raising must NOT 502 the endpoint: the txn
    # detail still renders with related=null + related_error=crm_unavailable.
    txn = _detail_txn(confirmed_ad_crm_id="REC777")
    _patch_db(monkeypatch, _GetSession(txn))

    def _boom(_id):
        raise RuntimeError("pymysql: (2003) Can't connect to MySQL server")

    monkeypatch.setattr(routes.crm_client, "get_ad", _boom)

    resp = client.get("/confirmed-ctl/bank-transaction/42")
    assert resp.status_code == 200
    data = resp.get_json()
    # The txn detail is fully present despite the CRM outage.
    assert data["txn_id"] == 42
    assert data["amount"] == -2226.94
    assert data["related"] is None
    assert data["related_error"] == "crm_unavailable"
    # No raw exception text leaked to the client.
    assert "pymysql" not in json.dumps(data)


# --------------------------------------------------------------------------- #
# _build_gmail_url — account-index-agnostic deep link (ad_number is ignored)
# --------------------------------------------------------------------------- #
def test_build_gmail_url_ignores_ad_number_and_uses_thread():
    url = routes._build_gmail_url(None, "THREAD123")
    assert url.startswith("https://mail.google.com/mail/?authuser=")
    assert url.endswith("#all/THREAD123")
    # ad_number is ignored: the URL is identical whether or not one is supplied,
    # and the literal string "None" is never injected into it.
    assert routes._build_gmail_url("AD-999", "THREAD123") == url
    assert "None" not in url


def test_build_gmail_url_empty_when_no_thread():
    # Empty / None thread id -> "" (never a URL, never the literal "None"),
    # regardless of the ignored ad_number argument.
    assert routes._build_gmail_url(None, None) == ""
    assert routes._build_gmail_url(None, "") == ""
    assert routes._build_gmail_url("AD-1", None) == ""
    assert routes._build_gmail_url("AD-1", "") == ""
