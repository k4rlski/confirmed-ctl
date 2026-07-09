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
    )
    monkeypatch.setattr(routes.crm_client, "get_ad", lambda _id: ad)
    monkeypatch.setattr(
        routes, "get_candidate_transactions",
        lambda db, ad: [{"transaction": _fake_txn(), "score": 0.91}],
    )
    monkeypatch.setattr(routes, "search_threads_by_ad_number", lambda n: [{"id": "t1"}])
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
    assert len(data["bank_candidates"]) == 1
    cand = data["bank_candidates"][0]
    assert cand["txn_id"] == 1
    assert cand["amount"] == 1368.0
    assert cand["score"] == 0.91
    assert data["gmail_threads"] == [{"id": "t1"}]


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
    monkeypatch.setattr(routes, "search_threads_by_ad_number", lambda n: [])
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


def test_candidates_survives_gmail_failure(client, monkeypatch):
    _configure_crm(monkeypatch)
    ad = CrmAd(crm_id="REC123", ad_number="IPR1", newspaper_name="Miami Herald",
               run_date=date(2026, 6, 15), expected_charge_date=date(2026, 6, 17),
               expected_amount=100.0)
    monkeypatch.setattr(routes.crm_client, "get_ad", lambda _id: ad)
    monkeypatch.setattr(routes, "get_candidate_transactions", lambda db, ad: [])

    def _boom(_n):
        raise RuntimeError("gmail down")

    monkeypatch.setattr(routes, "search_threads_by_ad_number", _boom)
    _patch_db(monkeypatch, object())

    resp = client.get("/confirmed-ctl/candidates/REC123")
    assert resp.status_code == 200
    assert resp.get_json()["gmail_threads"] == []


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
              attorney="John Atty", entity="PA"),
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
