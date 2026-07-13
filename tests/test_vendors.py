"""Tests for the ad-rep <-> bank merchant-string registry (vendor-map).

Pure helpers are tested directly; the CRUD routes are exercised against an
in-memory SQLite database holding ONLY the three new tables (the Postgres-only
``bank_transactions`` ARRAY/JSONB columns are not created here — the scan path is
smoke-tested live on fang). The Postgres session is monkeypatched to the SQLite
session and the CRM is never touched.
"""

from contextlib import contextmanager

import pytest

flask = pytest.importorskip("flask")

from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from confirmed_ctl import vendors  # noqa: E402
from confirmed_ctl.api import routes  # noqa: E402
from confirmed_ctl.db.models import (  # noqa: E402
    AdRep,
    AdRepMerchantLink,
    BankMerchantString,
)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def test_normalize_merchant_string():
    assert vendors.normalize_merchant_string("  dallas   morning  news ") == "DALLAS MORNING NEWS"
    assert vendors.normalize_merchant_string(None) == ""
    assert vendors.normalize_merchant_string("DALLAS MORNING NEWS-AD-DALLAS ,TX") == "DALLAS MORNING NEWS-AD-DALLAS ,TX"


def test_parse_email_header():
    d, e, dom = vendors.parse_email_header("Buchanan, Roshanda <Roshanda.Buchanan@MediumGiant.co>")
    assert e == "roshanda.buchanan@mediumgiant.co"
    assert dom == "mediumgiant.co"
    assert "Buchanan" in d
    d2, e2, dom2 = vendors.parse_email_header("bare@x.com")
    assert (d2, e2, dom2) == ("", "bare@x.com", "x.com")


# --------------------------------------------------------------------------- #
# Route CRUD against SQLite (3 new tables only)
# --------------------------------------------------------------------------- #
@pytest.fixture
def session():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    for tbl in (
        AdRep.__table__,
        BankMerchantString.__table__,
        AdRepMerchantLink.__table__,
    ):
        tbl.create(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)()


@pytest.fixture
def client(monkeypatch, session):
    @contextmanager
    def fake_get_db():
        yield session

    monkeypatch.setattr(routes, "get_db", fake_get_db)
    app = flask.Flask(__name__)
    app.register_blueprint(routes.confirmed_ctl_bp)
    return app.test_client()


def test_create_rep_and_idempotent(client):
    r = client.post("/confirmed-ctl/vendor-map/reps",
                    json={"email": "Buchanan, Roshanda <roshanda.buchanan@mediumgiant.co>"})
    assert r.status_code == 201
    body = r.get_json()
    assert body["created"] is True
    assert body["rep"]["email"] == "roshanda.buchanan@mediumgiant.co"
    assert body["rep"]["domain"] == "mediumgiant.co"

    # Same email again -> not created (idempotent), 200.
    r2 = client.post("/confirmed-ctl/vendor-map/reps",
                     json={"email": "roshanda.buchanan@mediumgiant.co"})
    assert r2.status_code == 200
    assert r2.get_json()["created"] is False


def test_create_rep_requires_email(client):
    r = client.post("/confirmed-ctl/vendor-map/reps", json={"email": ""})
    assert r.status_code == 400


def test_string_link_unlink_and_cascade(client):
    rep = client.post("/confirmed-ctl/vendor-map/reps",
                      json={"email": "roshanda.buchanan@mediumgiant.co"}).get_json()["rep"]
    s = client.post("/confirmed-ctl/vendor-map/strings",
                    json={"raw_string": "DALLAS MORNING NEWS-AD-DALLAS ,TX"}).get_json()["string"]
    assert s["normalized_string"] == "DALLAS MORNING NEWS-AD-DALLAS ,TX"
    assert s["source"] == "manual"

    # Link by ids.
    lk = client.post("/confirmed-ctl/vendor-map/links",
                     json={"ad_rep_id": rep["id"], "bank_merchant_string_id": s["id"]})
    assert lk.status_code == 201
    link = lk.get_json()["link"]
    assert link["ad_rep_email"] == "roshanda.buchanan@mediumgiant.co"
    assert link["normalized_string"] == "DALLAS MORNING NEWS-AD-DALLAS ,TX"

    # Duplicate link -> idempotent, not created.
    lk2 = client.post("/confirmed-ctl/vendor-map/links",
                      json={"ad_rep_id": rep["id"], "bank_merchant_string_id": s["id"]})
    assert lk2.get_json()["created"] is False

    # Overview shows the pairing and zero unlinked strings.
    ov = client.get("/confirmed-ctl/vendor-map").get_json()
    assert ov["counts"]["links"] == 1
    assert ov["counts"]["unlinked_strings"] == 0
    assert ov["reps"][0]["strings"][0]["normalized_string"] == "DALLAS MORNING NEWS-AD-DALLAS ,TX"

    # Unlink -> string becomes unlinked again.
    d = client.delete("/confirmed-ctl/vendor-map/links/%d" % link["id"])
    assert d.status_code == 200
    ov2 = client.get("/confirmed-ctl/vendor-map").get_json()
    assert ov2["counts"]["links"] == 0
    assert ov2["counts"]["unlinked_strings"] == 1


def test_link_inline_creation(client):
    """POST /links can create both sides inline (email + raw_string)."""
    lk = client.post("/confirmed-ctl/vendor-map/links", json={
        "email": "Rep One <rep.one@paper.com>",
        "raw_string": "SF CHRONICLE ADVTZNG -SAN FRANCISCO,CA",
    })
    assert lk.status_code == 201
    link = lk.get_json()["link"]
    assert link["ad_rep_email"] == "rep.one@paper.com"
    assert link["normalized_string"] == "SF CHRONICLE ADVTZNG -SAN FRANCISCO,CA"


def test_delete_rep_cascades_links(client):
    rep = client.post("/confirmed-ctl/vendor-map/reps",
                      json={"email": "x@y.com"}).get_json()["rep"]
    s = client.post("/confirmed-ctl/vendor-map/strings",
                    json={"raw_string": "NY POST ADVERTISING -NEW YORK ,NY"}).get_json()["string"]
    client.post("/confirmed-ctl/vendor-map/links",
                json={"ad_rep_id": rep["id"], "bank_merchant_string_id": s["id"]})

    dr = client.delete("/confirmed-ctl/vendor-map/reps/%d" % rep["id"])
    assert dr.status_code == 200
    ov = client.get("/confirmed-ctl/vendor-map").get_json()
    assert ov["counts"]["reps"] == 0
    assert ov["counts"]["links"] == 0
    # The string survives (only the rep + its links were removed).
    assert ov["counts"]["strings"] == 1


def test_edit_rep_notes(client):
    rep = client.post("/confirmed-ctl/vendor-map/reps",
                      json={"email": "z@q.com"}).get_json()["rep"]
    up = client.patch("/confirmed-ctl/vendor-map/reps/%d" % rep["id"],
                      json={"notes": "primary Dallas rep"})
    assert up.status_code == 200
    assert up.get_json()["rep"]["notes"] == "primary Dallas rep"
