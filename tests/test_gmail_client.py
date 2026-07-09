"""Unit tests for the read-only Gmail thread-surfacing client.

A fake Gmail ``service`` (no network) records the ``threads().list`` queries and
returns canned thread lists / metadata so we can assert query construction, the
date-windowed paper-name fallback, dedup+ranking, ``matched_by`` classification,
the account-index-agnostic ``gmail_url``, and the blank-ad-number guard.
"""

from datetime import date

import pytest

from confirmed_ctl import settings
from confirmed_ctl.gmail import client as gmail_client


class _Executable:
    def __init__(self, value):
        self._value = value

    def execute(self):
        return self._value


class _ThreadsResource:
    def __init__(self, fake):
        self._fake = fake

    def list(self, userId, q, maxResults, includeSpamTrash):
        self._fake.list_calls.append(
            {
                "q": q,
                "maxResults": maxResults,
                "includeSpamTrash": includeSpamTrash,
            }
        )
        threads = self._fake.query_results.get(q, [])
        return _Executable({"threads": [{"id": t} for t in threads]})

    def get(self, userId, id, format, metadataHeaders):
        return _Executable(self._fake.thread_details[id])


class _UsersResource:
    def __init__(self, fake):
        self._fake = fake

    def threads(self):
        return _ThreadsResource(self._fake)


class FakeService:
    def __init__(self, query_results, thread_details):
        # {query_string: [thread_id, ...]}
        self.query_results = query_results
        # {thread_id: threads().get(...) response dict}
        self.thread_details = thread_details
        self.list_calls: list[dict] = []

    def users(self):
        return _UsersResource(self)


def _detail(subject, snippet, *, from_="billing@paper.com", n=1):
    return {
        "snippet": snippet,
        "messages": [
            {
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": subject},
                        {"name": "From", "value": from_},
                        {"name": "Date", "value": "Tue, 16 Jun 2026 10:00:00 -0400"},
                    ]
                }
            }
        ]
        * n,
    }


def _install(monkeypatch, service):
    monkeypatch.setattr(gmail_client, "get_gmail_service", lambda: service)


def test_blank_ad_number_raises_value_error(monkeypatch):
    # Must be distinguishable from "searched, found nothing".
    called = {"n": 0}

    def _svc():
        called["n"] += 1
        return object()

    monkeypatch.setattr(gmail_client, "get_gmail_service", _svc)
    for bad in ("", "   ", "\t\n"):
        with pytest.raises(ValueError):
            gmail_client.search_threads_by_ad_number(bad)
    # Never even built the service (guarded before any Gmail call).
    assert called["n"] == 0


def test_adnum_only_no_paper_clause_when_charge_date_missing(monkeypatch):
    svc = FakeService(
        query_results={'"IPCSFC0022719"': ["t1"]},
        thread_details={"t1": _detail("Ad IPCSFC0022719 ran", "receipt")},
    )
    _install(monkeypatch, svc)

    out = gmail_client.search_threads_by_ad_number(
        "IPCSFC0022719  ", newspaper_name="SF Chronicle", charge_date=None
    )
    # Only the ad# clause was searched (no unbounded paper-name flood).
    assert [c["q"] for c in svc.list_calls] == ['"IPCSFC0022719"']
    assert len(out) == 1
    assert out[0]["thread_id"] == "t1"
    assert out[0]["matched_by"] == "ad_number"
    assert svc.list_calls[0]["includeSpamTrash"] is True


def test_date_windowed_paper_fallback_added_when_charge_date_present(monkeypatch):
    adnum_q = '"IPCSFC0022719"'
    paper_q = '"SF Chronicle" after:2026/06/03 before:2026/06/24'
    svc = FakeService(
        query_results={adnum_q: ["t1"], paper_q: ["t2"]},
        thread_details={
            "t1": _detail("Ad IPCSFC0022719 ran", "your ad IPCSFC0022719"),
            "t2": _detail("SF Chronicle receipt", "automated payment receipt"),
        },
    )
    _install(monkeypatch, svc)

    out = gmail_client.search_threads_by_ad_number(
        "IPCSFC0022719",
        newspaper_name="SF Chronicle",
        charge_date=date(2026, 6, 17),
    )
    queries = [c["q"] for c in svc.list_calls]
    assert adnum_q in queries
    assert paper_q in queries  # charge-14d .. charge+7d window
    # ad#-matched thread ranked first, paper-name-only second.
    assert [t["thread_id"] for t in out] == ["t1", "t2"]
    assert out[0]["matched_by"] == "ad_number"
    assert out[1]["matched_by"] == "paper_name"


def test_results_deduped_and_capped(monkeypatch):
    adnum_q = '"AD1"'
    paper_q = '"Paper" after:2026/06/03 before:2026/06/24'
    # t1 appears in BOTH searches -> must appear once, ranked first (ad#).
    svc = FakeService(
        query_results={adnum_q: ["t1"], paper_q: ["t1", "t2", "t3"]},
        thread_details={
            "t1": _detail("Ad AD1", "AD1 receipt"),
            "t2": _detail("Paper receipt", "auto receipt"),
            "t3": _detail("Paper receipt 2", "auto receipt 2"),
        },
    )
    _install(monkeypatch, svc)

    out = gmail_client.search_threads_by_ad_number(
        "AD1", newspaper_name="Paper", charge_date=date(2026, 6, 17), max_results=2
    )
    ids = [t["thread_id"] for t in out]
    assert ids == ["t1", "t2"]  # deduped (t1 once), ad#-first, capped at 2


def test_gmail_url_uses_authuser_all_and_impersonate(monkeypatch):
    monkeypatch.setattr(settings, "GMAIL_IMPERSONATE", "karl@perm-ads.com")
    svc = FakeService(
        query_results={'"AD1"': ["tXYZ"]},
        thread_details={"tXYZ": _detail("Ad AD1", "AD1")},
    )
    _install(monkeypatch, svc)

    out = gmail_client.search_threads_by_ad_number("AD1")
    assert out[0]["gmail_url"] == (
        "https://mail.google.com/mail/?authuser=karl@perm-ads.com#all/tXYZ"
    )
    # Existing keys preserved alongside the new ones.
    for key in ("thread_id", "subject", "from", "date", "snippet", "message_count"):
        assert key in out[0]
    assert "gmail_url" in out[0]
    assert "matched_by" in out[0]


def test_coerce_charge_date_accepts_common_forms():
    assert gmail_client._coerce_charge_date(date(2026, 6, 17)) == date(2026, 6, 17)
    assert gmail_client._coerce_charge_date("2026-06-17") == date(2026, 6, 17)
    assert gmail_client._coerce_charge_date("06/17/2026") == date(2026, 6, 17)
    assert gmail_client._coerce_charge_date(None) is None
    assert gmail_client._coerce_charge_date("") is None
    assert gmail_client._coerce_charge_date("not-a-date") is None
