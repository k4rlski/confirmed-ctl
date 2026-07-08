"""Tests for confirmed_ctl.settings env parsing.

Focus: CRM_DB_PORT must be robust. A blank ``CRM_DB_PORT=`` (present but empty)
previously crashed ``int("")`` with a ValueError at import; the parser now
falls back to the default 3306. We exercise the ``_get_int`` helper directly
(it reads os.environ on each call) for the blank / unset / valid-custom cases.
"""

from confirmed_ctl import settings


def test_get_int_unset_uses_default(monkeypatch):
    monkeypatch.delenv("CRM_DB_PORT", raising=False)
    assert settings._get_int("CRM_DB_PORT", 3306) == 3306


def test_get_int_blank_uses_default(monkeypatch):
    monkeypatch.setenv("CRM_DB_PORT", "")
    assert settings._get_int("CRM_DB_PORT", 3306) == 3306


def test_get_int_whitespace_uses_default(monkeypatch):
    monkeypatch.setenv("CRM_DB_PORT", "   ")
    assert settings._get_int("CRM_DB_PORT", 3306) == 3306


def test_get_int_valid_custom_port(monkeypatch):
    monkeypatch.setenv("CRM_DB_PORT", "3307")
    assert settings._get_int("CRM_DB_PORT", 3306) == 3307


def test_get_int_invalid_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("CRM_DB_PORT", "not-a-port")
    assert settings._get_int("CRM_DB_PORT", 3306) == 3306


def test_crm_db_port_default_is_3306():
    # The module-level constant parses to the default when unset in this env.
    assert isinstance(settings.CRM_DB_PORT, int)
    assert settings.CRM_DB_PORT == 3306
