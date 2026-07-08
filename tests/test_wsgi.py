"""Tests for the WSGI entrypoint (confirmed_ctl.wsgi).

Covers the bearer-token auth layer installed in ``create_app`` via
``before_request``: /healthz is exempt, protected routes require a correct
Bearer token, wrong tokens are rejected, and an unset token fails open.

No live CRM/Postgres/Gmail: the CRM adapter is forced "unconfigured" so the
protected route short-circuits to a 503 (proving it passed the auth layer
without a 401) instead of making any live call.
"""

import pytest

pytest.importorskip("flask")

from confirmed_ctl import wsgi  # noqa: E402
from confirmed_ctl.api import routes  # noqa: E402

TEST_TOKEN = "test-secret-token-123"


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(wsgi.settings, "API_TOKEN", TEST_TOKEN)
    return wsgi.create_app()


@pytest.fixture
def client(app):
    return app.test_client()


def _force_crm_unconfigured(monkeypatch):
    """Ensure protected routes never touch a live CRM during auth tests."""
    monkeypatch.setattr(routes.crm_client, "is_configured", lambda: False)


# --------------------------------------------------------------------------- #
# /healthz — exempt from auth
# --------------------------------------------------------------------------- #
def test_healthz_ok_without_token(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


# --------------------------------------------------------------------------- #
# Protected routes require a bearer token
# --------------------------------------------------------------------------- #
def test_protected_route_401_without_authorization(client):
    resp = client.get("/confirmed-ctl/unconfirmed")
    assert resp.status_code == 401
    assert resp.get_json() == {"error": "unauthorized"}


def test_protected_route_passes_with_correct_token(client, monkeypatch):
    _force_crm_unconfigured(monkeypatch)
    resp = client.get(
        "/confirmed-ctl/unconfirmed",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )
    # Passed the auth layer: not a 401. With CRM unconfigured it becomes a 503
    # (crm_not_configured), never a live call.
    assert resp.status_code != 401
    assert resp.status_code == 503
    assert resp.get_json()["status"] == "crm_not_configured"


def test_protected_route_401_with_wrong_token(client):
    resp = client.get(
        "/confirmed-ctl/unconfirmed",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401
    assert resp.get_json() == {"error": "unauthorized"}


def test_protected_route_401_with_malformed_header(client):
    # Missing the "Bearer " scheme prefix.
    resp = client.get(
        "/confirmed-ctl/unconfirmed",
        headers={"Authorization": TEST_TOKEN},
    )
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# Fail-open when token unset
# --------------------------------------------------------------------------- #
def test_fail_open_when_token_empty(monkeypatch):
    monkeypatch.setattr(wsgi.settings, "API_TOKEN", "")
    _force_crm_unconfigured(monkeypatch)
    client = wsgi.create_app().test_client()

    resp = client.get("/confirmed-ctl/unconfirmed")
    # No auth enforced -> request passes the guard (no 401); CRM unconfigured
    # -> 503, still no live call.
    assert resp.status_code != 401
    assert resp.status_code == 503


def test_healthz_ok_when_token_set(client):
    # /healthz stays exempt even when a token is configured.
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_module_level_app_exists():
    from flask import Flask

    assert isinstance(wsgi.app, Flask)
