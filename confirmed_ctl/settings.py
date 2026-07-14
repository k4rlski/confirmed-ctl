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


def _get_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean env var (truthy: 1/true/yes/on, case-insensitive).

    Returns ``default`` when unset, blank, or whitespace-only.
    """
    raw = _get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _get_int(name: str, default: int) -> int:
    """Parse an integer env var robustly.

    Returns ``default`` when the variable is unset, blank, or whitespace-only,
    and also falls back to ``default`` (rather than raising) when the value is
    present but not a valid integer. This keeps a blank ``CRM_DB_PORT=`` in a
    systemd/cron env from crashing startup with a ``ValueError``.
    """
    raw = _get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


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
CRM_DB_PORT = _get_int("CRM_DB_PORT", 3306)

# CRM WRITE-BACK gate (default FALSE => read-only everywhere; dev/test NEVER
# write to the live CRM). When true, /confirm issues ONE strictly-allowlisted
# UPDATE to the matched ``t_e_s_t_p_e_r_m`` record via
# ``confirmed_ctl.crm.client.update_ad_clearance`` (columns statclearancenews,
# trxstring, urlgmailadconfirm — nothing else; the staff-owned datepaidnews is
# never written). Set ``CONFIRMED_CTL_CRM_WRITE=true`` ONLY on the fang service,
# where the ``permtrak2_crm`` user is granted from fang's IP.
CRM_WRITE_ENABLED = _get_bool("CONFIRMED_CTL_CRM_WRITE", False)

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

# Ad-rep Gmail scan (seeds ``ad_reps`` from ad-confirmation From headers — see
# confirmed_ctl/ingest/rep_scan.py). Read-only, same SA/mailbox as the BofA scan.
#   AD_REP_SCAN_LOOKBACK_DAYS — default window (days) for `vendors scan-reps`.
#   AD_REP_SCAN_QUERY — Gmail query defining the ad-confirmation universe. Blank
#     => the built-in default (exclude the BofA alert sender); narrow it (e.g. a
#     label: or a set of from: clauses) once the real rep sender set is known.
#   AD_REP_SKIP_DOMAINS — extra comma-separated internal domains to drop from the
#     From harvest (the perm-ads.com family + the bank are always skipped).
#   AD_REP_SCAN_MAX_MESSAGES — hard cap on messages fetched per scan. Gmail
#     returns newest-first, so with a broad default query this samples the most
#     recent slice instead of fetching thousands one-by-one (each is a round-trip).
#     Narrow AD_REP_SCAN_QUERY for full coverage of the finite charge universe.
AD_REP_SCAN_LOOKBACK_DAYS = _get_int("AD_REP_SCAN_LOOKBACK_DAYS", 30)
AD_REP_SCAN_QUERY = _get("AD_REP_SCAN_QUERY", "")
AD_REP_SKIP_DOMAINS = _get("AD_REP_SKIP_DOMAINS", "")
AD_REP_SCAN_MAX_MESSAGES = _get_int("AD_REP_SCAN_MAX_MESSAGES", 300)

# Candidate-matching window (days) for the scorer (matching/scorer.py). Bank
# charges can post before OR after the CRM buy/charge date, so the window spans
# ``[charge_date - LOOKBACK, charge_date + LOOKAHEAD]``. Defaults are wider (10/10)
# than the old hardcoded 5/2 so a charge that posts a week+ off the expected date
# still surfaces as a candidate. Both are env-overridable with a robust int parse.
MATCH_LOOKBACK_DAYS = _get_int("CONFIRMED_CTL_MATCH_LOOKBACK_DAYS", 10)
MATCH_LOOKAHEAD_DAYS = _get_int("CONFIRMED_CTL_MATCH_LOOKAHEAD_DAYS", 10)

# Receipts + RAG storage. Default is a real on-host data dir (NOT the old
# placeholder /mnt/receipts, which was never provisioned); override per host via
# the RECEIPTS_BASE_PATH env. Receipt PDFs are laid out as <base>/<YYYY>/<MM>/<ad>.
RECEIPTS_BASE_PATH = _get("RECEIPTS_BASE_PATH", "/var/lib/confirmed-ctl/receipts")
CHROMA_PATH = _get("CHROMA_PATH", "/opt/confirmed-ctl/chroma_db")

# Daemon
SYNC_INTERVAL_SECONDS = int(_get("SYNC_INTERVAL_SECONDS", "3600"))

# HTTP API (confirmed_ctl.wsgi:app served by gunicorn on fang, reached from
# claw/MARS over an SSH tunnel).
# API_TOKEN gates every request via a bearer-token before_request guard in
# wsgi.py. IMPORTANT fail-open-when-unset behavior: when this is empty
# (dev/test/unconfigured) the guard allows all requests through; the fang
# service MUST set CONFIRMED_CTL_API_TOKEN so production is authenticated.
API_TOKEN = os.environ.get("CONFIRMED_CTL_API_TOKEN", "")

# Fail-CLOSED switch. When true AND API_TOKEN is empty, the guard REFUSES to
# serve non-exempt routes (503 auth_required_but_unset) instead of failing open.
# Default false preserves the fail-open-when-unset dev/test behavior (which then
# emits a loud warning). /healthz stays exempt in all cases.
REQUIRE_AUTH = _get_bool("CONFIRMED_CTL_REQUIRE_AUTH", False)

# Address the API binds to. Localhost-only by design: the API is never exposed
# publicly — claw/MARS reach it through an SSH tunnel. Used for documentation
# and the systemd unit / bind string.
API_BIND = os.environ.get("CONFIRMED_CTL_API_BIND", "127.0.0.1:8787")
