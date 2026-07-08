"""Environment-driven settings for confirmed-ctl.

Values come from process environment variables (optionally loaded from a local
``.env`` file via python-dotenv). See ``.env.example`` for the full list.
"""

from __future__ import annotations

import os

try:  # dotenv is optional; env vars may be provided by systemd/cron directly.
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv missing or unreadable .env
    pass


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


# Database
DATABASE_URL = _get("DATABASE_URL")

# QBO / QuickBooks Online
QBO_CLIENT_ID = _get("QBO_CLIENT_ID")
QBO_CLIENT_SECRET = _get("QBO_CLIENT_SECRET")
QBO_REALM_ID = _get("QBO_REALM_ID")
QBO_TOKEN_PATH = _get("QBO_TOKEN_PATH", "/opt/confirmed-ctl/qbo_tokens.json")
QBO_API_BASE_URL = _get("QBO_API_BASE_URL", "https://quickbooks.api.intuit.com")
QBO_TOKEN_ENDPOINT = _get(
    "QBO_TOKEN_ENDPOINT",
    "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
)
QBO_MINOR_VERSION = _get("QBO_MINOR_VERSION", "73")

# Gmail
GMAIL_TOKEN_PATH = _get("GMAIL_TOKEN_PATH", "/opt/confirmed-ctl/gmail_token.json")

# Receipts + RAG storage
RECEIPTS_BASE_PATH = _get("RECEIPTS_BASE_PATH", "/mnt/receipts")
CHROMA_PATH = _get("CHROMA_PATH", "/opt/confirmed-ctl/chroma_db")

# Daemon
SYNC_INTERVAL_SECONDS = int(_get("SYNC_INTERVAL_SECONDS", "3600"))
