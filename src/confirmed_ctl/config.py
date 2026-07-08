"""Configuration loading for confirmed-ctl.

Loads ``confirmed-ctl.yml`` (see ``confirmed-ctl.yml.example``) into typed
dataclasses. Environment variables of the form ``CONFIRMED_CTL_<SECTION>_<KEY>``
override file values so credentials can be injected without editing the file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_NAMES = ("confirmed-ctl.yml", "confirmed-ctl.yaml")


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


@dataclass
class CrmConfig:
    host: str = "permtrak.com"
    port: int = 3306
    database: str = "permtrak2_crm"
    user: str = ""
    password: str = ""
    cases_table: str = "t_e_s_t_p_e_r_m"
    news_table: str = "news"
    trigger_statuses: list[str] = field(
        default_factory=lambda: ["Confirmed", "PaymentConfirmed"]
    )
    done_status: str = "Done"


@dataclass
class GmailConfig:
    gmail_ctl_path: str = "/opt/auto-cmd/gmail-ctl"
    accounts: list[str] = field(
        default_factory=lambda: ["auto-ctl@perm-ads.com", "info@perm-ads.com"]
    )


@dataclass
class PlaidConfig:
    plaid_ctl_path: str = "/opt/auto-cmd/plaid-ctl"
    default_window_hours: int = 48
    amount_tolerance: float = 1.00


@dataclass
class DropboxConfig:
    remote: str = "dropbox"
    base_path: str = "Receipts/Newspapers"
    staging_dir: str = "./receipts_tmp"


@dataclass
class SlackConfig:
    webhook_url: str = ""
    channel: str = "#reports-ctl"


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = ""


@dataclass
class Config:
    crm: CrmConfig = field(default_factory=CrmConfig)
    gmail: GmailConfig = field(default_factory=GmailConfig)
    plaid: PlaidConfig = field(default_factory=PlaidConfig)
    dropbox: DropboxConfig = field(default_factory=DropboxConfig)
    slack: SlackConfig = field(default_factory=SlackConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    source_path: str | None = None


def _section(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"Config section '{key}' must be a mapping, got {type(value).__name__}")
    return value


def _apply_env_overrides(section: str, values: dict[str, Any]) -> dict[str, Any]:
    """Override values from CONFIRMED_CTL_<SECTION>_<KEY> environment variables."""
    prefix = f"CONFIRMED_CTL_{section.upper()}_"
    for env_key, env_val in os.environ.items():
        if not env_key.startswith(prefix):
            continue
        field_name = env_key[len(prefix):].lower()
        values[field_name] = env_val
    return values


def find_config_path(explicit: str | None = None) -> Path | None:
    """Locate the config file: explicit path, CWD, or the package parent dir."""
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            raise ConfigError(f"Config file not found: {p}")
        return p
    search_dirs = [Path.cwd(), Path(__file__).resolve().parent.parent.parent]
    for directory in search_dirs:
        for name in DEFAULT_CONFIG_NAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def load_config(path: str | None = None) -> Config:
    """Load configuration. Missing files yield defaults (useful for --dry-run)."""
    config_path = find_config_path(path)
    data: dict[str, Any] = {}
    if config_path is not None:
        with open(config_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ConfigError(f"Top-level config in {config_path} must be a mapping")

    cfg = Config(
        crm=CrmConfig(**_coerce(CrmConfig, _apply_env_overrides("crm", _section(data, "crm")))),
        gmail=GmailConfig(
            **_coerce(GmailConfig, _apply_env_overrides("gmail", _section(data, "gmail")))
        ),
        plaid=PlaidConfig(
            **_coerce(PlaidConfig, _apply_env_overrides("plaid", _section(data, "plaid")))
        ),
        dropbox=DropboxConfig(
            **_coerce(DropboxConfig, _apply_env_overrides("dropbox", _section(data, "dropbox")))
        ),
        slack=SlackConfig(
            **_coerce(SlackConfig, _apply_env_overrides("slack", _section(data, "slack")))
        ),
        logging=LoggingConfig(
            **_coerce(LoggingConfig, _apply_env_overrides("logging", _section(data, "logging")))
        ),
        source_path=str(config_path) if config_path else None,
    )
    return cfg


def _coerce(cls: type, values: dict[str, Any]) -> dict[str, Any]:
    """Keep only known fields and coerce simple scalar types from env strings."""
    annotations = getattr(cls, "__annotations__", {})
    result: dict[str, Any] = {}
    for key, value in values.items():
        if key not in annotations:
            continue
        target = annotations[key]
        if isinstance(value, str) and target in (int, "int"):
            value = int(value)
        elif isinstance(value, str) and target in (float, "float"):
            value = float(value)
        result[key] = value
    return result
