"""Endpoint tests for /candidates and /unconfirmed.

The CRM adapter and the Postgres session are mocked — no live CRM, no live DB.
We exercise the real Flask routing/JSON via a test client and assert the
configured/unconfigured/not-found branches and the unconfirmed set-subtraction.
"""

from contextlib import contextmanager
from datetime import date

import pytest

flask = pytest.importorskip("flask")

from confirmed_ctl.api import routes  # noqa: E402
from confirmed_ctl.db.models import BankTransaction, CrmAd  # noqa: E402


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
              run_end=date(2026, 6, 10), status_news='["Active"]', owner="karl"),
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
