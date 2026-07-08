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

# CRM (MariaDB permtrak2_crm, READ-ONLY). The read-only lookup adapter in
# confirmed_ctl/crm/client.py connects with these; when CRM_DB_HOST is empty the
# adapter is treated as "not configured" and the /candidates & /unconfirmed
# endpoints return 503 rather than crashing. Never write to the CRM from here.
CRM_DB_HOST = _get("CRM_DB_HOST")
CRM_DB_USER = _get("CRM_DB_USER")
CRM_DB_PASS = _get("CRM_DB_PASS")
CRM_DB_NAME = _get("CRM_DB_NAME")
CRM_DB_PORT = int(_get("CRM_DB_PORT", "3306"))

# Gmail
# GMAIL_TOKEN_PATH points at the Google **service-account** JSON key file used for
# read-only, domain-wide-delegated access (impersonating GMAIL_IMPERSONATE). The
# path is a secret location on disk — never hardcode the key contents here.
GMAIL_TOKEN_PATH = _get(
    "GMAIL_TOKEN_PATH", "/opt/confirmed-ctl/secrets/google-service-account.json"
)
# The mailbox the service account impersonates (domain-wide delegation subject).
# Default karl@perm-ads.com: it holds the BofA alerts in its DURABLE INBOX (not
# Trash) and also receives Paul's info@ vendor ad-confirmation traffic that this
# tool searches by CRM ad number. Override via env for info@perm-ads.com (the
# delivery address, which auto-trashes alerts) or the admin mailbox.
GMAIL_IMPERSONATE = _get("GMAIL_IMPERSONATE", "karl@perm-ads.com")

# Email-scan ingestion
# Default lookback window (days) for the BofA transaction-alert email scan.
EMAIL_SCAN_LOOKBACK_DAYS = int(_get("EMAIL_SCAN_LOOKBACK_DAYS", "2"))

# Receipts + RAG storage
RECEIPTS_BASE_PATH = _get("RECEIPTS_BASE_PATH", "/mnt/receipts")
CHROMA_PATH = _get("CHROMA_PATH", "/opt/confirmed-ctl/chroma_db")

# Daemon
SYNC_INTERVAL_SECONDS = int(_get("SYNC_INTERVAL_SECONDS", "3600"))
