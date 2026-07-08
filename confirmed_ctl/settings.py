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

# Gmail
GMAIL_TOKEN_PATH = _get("GMAIL_TOKEN_PATH", "/opt/confirmed-ctl/gmail_token.json")

# Receipts + RAG storage
RECEIPTS_BASE_PATH = _get("RECEIPTS_BASE_PATH", "/mnt/receipts")
CHROMA_PATH = _get("CHROMA_PATH", "/opt/confirmed-ctl/chroma_db")

# Daemon
SYNC_INTERVAL_SECONDS = int(_get("SYNC_INTERVAL_SECONDS", "3600"))
