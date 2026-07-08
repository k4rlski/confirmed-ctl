import textwrap

import pytest

from confirmed_ctl.config import ConfigError, load_config


def test_load_defaults_when_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg.crm.host == "permtrak.com"
    assert cfg.crm.trigger_statuses == ["Confirmed", "PaymentConfirmed"]
    assert cfg.dropbox.remote == "dropbox"


def test_load_from_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "confirmed-ctl.yml").write_text(
        textwrap.dedent(
            """
            crm:
              user: karl
              password: secret
              port: 3307
            dropbox:
              remote: db2
            """
        )
    )
    cfg = load_config()
    assert cfg.crm.user == "karl"
    assert cfg.crm.password == "secret"
    assert cfg.crm.port == 3307
    assert cfg.dropbox.remote == "db2"


def test_env_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONFIRMED_CTL_CRM_PASSWORD", "from-env")
    monkeypatch.setenv("CONFIRMED_CTL_CRM_PORT", "3399")
    cfg = load_config()
    assert cfg.crm.password == "from-env"
    assert cfg.crm.port == 3399


def test_explicit_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(str(tmp_path / "does-not-exist.yml"))
