"""Tests for the ad-rep Gmail scan (confirmed_ctl/ingest/rep_scan.py).

Query building + From-header harvesting are exercised offline with a fake Gmail
service (no live Gmail). The upsert path runs against an in-memory SQLite DB
holding ONLY the registry + sync-log tables (the Postgres-only JSONB/ARRAY bank
columns are not created here). The CRM is never touched.
"""

from datetime import date

import pytest

from confirmed_ctl import settings
from confirmed_ctl.ingest import rep_scan


# --------------------------------------------------------------------------- #
# Query building
# --------------------------------------------------------------------------- #
def test_build_query_default_excludes_bofa_and_bounds_by_epoch(monkeypatch):
    monkeypatch.setattr(settings, "AD_REP_SCAN_QUERY", "")
    q = rep_scan.build_query(30, today=date(2026, 7, 13))
    assert q.startswith(f"-from:{rep_scan.BOFA_SENDER}")
    assert "after:" in q


def test_build_query_honors_override():
    q = rep_scan.build_query(7, base_query="label:ad-confirmations", today=date(2026, 7, 13))
    assert q.startswith("label:ad-confirmations after:")


def test_skip_domains_includes_impersonate_and_env(monkeypatch):
    monkeypatch.setattr(settings, "GMAIL_IMPERSONATE", "karl@perm-ads.com")
    monkeypatch.setattr(settings, "AD_REP_SKIP_DOMAINS", "noreply.example.com, foo.com")
    domains = rep_scan.skip_domains()
    assert "perm-ads.com" in domains          # impersonate domain
    assert "bankofamerica.com" in domains      # always-skip
    assert "noreply.example.com" in domains    # env extension
    assert "foo.com" in domains


# --------------------------------------------------------------------------- #
# Fake Gmail service + harvest
# --------------------------------------------------------------------------- #
class _FakeGmail:
    """Minimal fake matching confirmed_ctl.gmail.client's usage surface.

    ``search_messages(service, query)`` yields stubs; ``get_message`` returns a
    message whose payload headers carry the From; ``get_headers`` lower-cases.
    """

    def __init__(self, messages):
        # messages: list of (id, from_header)
        self._messages = messages

    # gmail_client.search_messages(service, query) -> iterator of {id, threadId}
    def search_messages(self, service, query, max_results=2000):
        for mid, _frm in self._messages:
            yield {"id": mid, "threadId": "t-" + mid}

    def get_message(self, service, message_id, fmt="full"):
        frm = dict(self._messages)[message_id]
        return {"payload": {"headers": [{"name": "From", "value": frm}]}}

    def get_headers(self, message):
        return {
            h["name"].lower(): h["value"]
            for h in message.get("payload", {}).get("headers", [])
        }


@pytest.fixture
def fake_gmail(monkeypatch):
    def _install(messages):
        fake = _FakeGmail(messages)
        monkeypatch.setattr(rep_scan, "settings", settings)
        # Patch the gmail client module functions used by scan_rep_messages.
        from confirmed_ctl.gmail import client as gmail_client

        monkeypatch.setattr(gmail_client, "search_messages", fake.search_messages)
        monkeypatch.setattr(gmail_client, "get_message", fake.get_message)
        monkeypatch.setattr(gmail_client, "get_headers", fake.get_headers)
        return fake

    return _install


def test_scan_rep_messages_harvests_external_only(fake_gmail, monkeypatch):
    monkeypatch.setattr(settings, "GMAIL_IMPERSONATE", "karl@perm-ads.com")
    monkeypatch.setattr(settings, "AD_REP_SKIP_DOMAINS", "")
    fake_gmail([
        ("m1", "Buchanan, Roshanda <roshanda.buchanan@mediumgiant.co>"),
        ("m2", "Karl <karl@perm-ads.com>"),          # internal -> dropped
        ("m3", "BofA <onlinebanking@ealerts.bankofamerica.com>"),  # bank -> dropped
        ("m4", "Sales <sales@dallasnews.example>"),
        ("m5", "roshanda.buchanan@mediumgiant.co"),  # duplicate -> collapsed
    ])
    reps = rep_scan.scan_rep_messages(service=object(), lookback_days=30)
    emails = sorted(r.email for r in reps)
    assert emails == ["roshanda.buchanan@mediumgiant.co", "sales@dallasnews.example"]
    # Display name parsed off the header.
    roshanda = next(r for r in reps if r.email == "roshanda.buchanan@mediumgiant.co")
    assert "Buchanan" in roshanda.display_name


# --------------------------------------------------------------------------- #
# run_rep_scan upsert against SQLite
# --------------------------------------------------------------------------- #
@pytest.fixture
def session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from confirmed_ctl.db.models import AdRep, SyncLog

    engine = create_engine("sqlite://")
    for tbl in (AdRep.__table__, SyncLog.__table__):
        tbl.create(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)()


def test_run_rep_scan_upserts_and_is_idempotent(fake_gmail, monkeypatch, session):
    from confirmed_ctl.db.models import AdRep, SyncLog

    monkeypatch.setattr(settings, "GMAIL_IMPERSONATE", "karl@perm-ads.com")
    monkeypatch.setattr(settings, "AD_REP_SKIP_DOMAINS", "")
    fake_gmail([
        ("m1", "Roshanda <roshanda.buchanan@mediumgiant.co>"),
        ("m2", "Sales <sales@dallasnews.example>"),
    ])

    result = rep_scan.run_rep_scan(session, lookback_days=30, service=object())
    assert result["source"] == "rep-scan"
    assert result["found"] == 2
    assert result["created"] == 2
    assert result["existing"] == 0
    assert result["upserted"] == 2
    assert result["linked_proposed"] == 0
    assert session.query(AdRep).count() == 2
    # A SyncLog audit row was written for the run.
    assert session.query(SyncLog).filter(SyncLog.source == "rep-scan").count() == 1

    # Re-run -> idempotent: no new reps, both count as existing.
    result2 = rep_scan.run_rep_scan(session, lookback_days=30, service=object())
    assert result2["created"] == 0
    assert result2["existing"] == 2
    assert session.query(AdRep).count() == 2
