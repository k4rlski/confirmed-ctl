from datetime import datetime, timedelta, timezone

import pytest

from confirmed_ctl import settings
from confirmed_ctl.qbo import client


def test_is_expired_missing_expiry():
    assert client._is_expired({}) is True


def test_is_expired_past_and_future():
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert client._is_expired({"expires_at": past}) is True
    assert client._is_expired({"expires_at": future}) is False


def test_is_expired_within_skew():
    soon = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    # 30s < 120s refresh skew -> treated as expired
    assert client._is_expired({"expires_at": soon}) is True


def test_company_url(monkeypatch):
    monkeypatch.setattr(settings, "QBO_REALM_ID", "12345")
    monkeypatch.setattr(settings, "QBO_API_BASE_URL", "https://quickbooks.api.intuit.com")
    url = client._company_url("query")
    assert url == "https://quickbooks.api.intuit.com/v3/company/12345/query"


def test_company_url_requires_realm(monkeypatch):
    monkeypatch.setattr(settings, "QBO_REALM_ID", "")
    with pytest.raises(client.QboError):
        client._company_url("query")


def test_load_tokens_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "QBO_TOKEN_PATH", str(tmp_path / "nope.json"))
    with pytest.raises(client.QboError):
        client._load_tokens()
