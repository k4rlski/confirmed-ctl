"""confirmed_ctl/crm/client.py

Read-only (SELECT-only) adapter into the MariaDB CRM ``permtrak2_crm`` on
``permtrak.com``. Ad / case data lives ONLY in the CRM (see
``confirmed_ctl/db/models.py``); this module reads it into lightweight
:class:`~confirmed_ctl.db.models.CrmAd` read views and NEVER writes.

Connection pattern mirrors the mars-status reports helper: ``pymysql.connect``
with a ``DictCursor``, ``connect_timeout=10`` and ``read_timeout=60``, driven by
the ``CRM_DB_*`` settings. CRM write-back (statclearancenews -> '["Done"]',
trxstring, urlgmailadconfirm, datepaidnews) is intentionally NOT implemented
here — it is a separate later generation.
"""

from __future__ import annotations

import json

from .. import settings
from ..db.models import CrmAd

# ---------------------------------------------------------------------------
# SQL — the ABCF-X clearances query is reused VERBATIM. The SELECT column list
# and FROM/JOIN are shared so ``get_ad`` returns the exact same shape as a single
# clearances row (only the WHERE differs).
# ---------------------------------------------------------------------------
_SELECT_FROM = """SELECT news.owner AS owner, t_e_s_t_p_e_r_m.id, t_e_s_t_p_e_r_m.adsapproveddate,
       t_e_s_t_p_e_r_m.datebuynews, t_e_s_t_p_e_r_m.datenewsstart, t_e_s_t_p_e_r_m.name,
       t_e_s_t_p_e_r_m.jobtitle, t_e_s_t_p_e_r_m.attyname, t_e_s_t_p_e_r_m.beneficiarylast,
       t_e_s_t_p_e_r_m.entity, t_e_s_t_p_e_r_m.statclearancenews, t_e_s_t_p_e_r_m.statnews,
       t_e_s_t_p_e_r_m.statacctgcreditnews, t_e_s_t_p_e_r_m.dboxemailthreadcase,
       t_e_s_t_p_e_r_m.adnumbernews, news.name AS newspapers_name, news.rank,
       t_e_s_t_p_e_r_m.pricenewsreal
FROM t_e_s_t_p_e_r_m
JOIN news ON t_e_s_t_p_e_r_m.news_id = news.id"""

# The ABCF-X clearances WHERE clause, VERBATIM. EspoCRM stores enum fields as
# JSON string arrays, so we match those exact string forms.
_CLEARANCES_WHERE = """WHERE statnews='["Active"]' AND (entity='JKT' OR entity='PA')
  AND statclearancenews='["Confirmed"]' AND t_e_s_t_p_e_r_m.deleted=0
  AND t_e_s_t_p_e_r_m.statpermcase='["Active Case"]'
ORDER BY datebuynews DESC"""

CLEARANCES_QUERY = f"{_SELECT_FROM}\n{_CLEARANCES_WHERE}"

# Single-ad lookup: same SELECT columns, parameterized by EspoCRM record id.
# ``%s`` is bound by pymysql — NEVER string-interpolate ``ad_crm_id``.
GET_AD_QUERY = f"{_SELECT_FROM}\nWHERE t_e_s_t_p_e_r_m.id=%s AND t_e_s_t_p_e_r_m.deleted=0"


def is_configured() -> bool:
    """True when a CRM host is configured. When False the adapter is a no-op and
    callers should surface a clear "CRM not configured" (503) rather than crash.
    """
    return bool(settings.CRM_DB_HOST)


def parse_enum(value) -> str | None:
    """Parse an EspoCRM enum field into a plain string.

    EspoCRM stores enums as JSON string arrays, e.g. ``'["Confirmed"]'``. This
    returns the first element (``'Confirmed'``). It is tolerant of plain strings
    (returned stripped) and of ``None`` (returned as ``None``).
    """
    if value is None:
        return None
    if isinstance(value, list):
        return str(value[0]) if value else None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            return text
        if isinstance(parsed, list):
            return str(parsed[0]) if parsed else None
        return str(parsed)
    return text


def _connect():
    """Open a read-only pymysql connection to the CRM.

    Uses a ``DictCursor`` with ``connect_timeout=10`` / ``read_timeout=60``,
    mirroring the mars-status reports helper. Callers use it for SELECT only.
    """
    import pymysql

    return pymysql.connect(
        host=settings.CRM_DB_HOST,
        port=settings.CRM_DB_PORT,
        user=settings.CRM_DB_USER,
        password=settings.CRM_DB_PASS,
        database=settings.CRM_DB_NAME,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=60,
    )


def _row_to_crm_ad(row: dict) -> CrmAd:
    """Map one CRM result row (DictCursor) to a :class:`CrmAd` read view."""
    return CrmAd(
        crm_id=str(row["id"]) if row.get("id") is not None else None,
        ad_number=row.get("adnumbernews"),
        client_name=row.get("name"),
        newspaper_name=row.get("newspapers_name"),
        run_date=row.get("datenewsstart"),
        # Charge date is the "date buy news"; fall back to the run start date.
        expected_charge_date=row.get("datebuynews") or row.get("datenewsstart"),
        expected_amount=row.get("pricenewsreal"),
    )


def list_clearances() -> list[CrmAd]:
    """Return every confirmed, active JKT/PA clearance as a :class:`CrmAd`.

    Runs the ABCF-X clearances query verbatim (read-only). Returns an empty list
    when the CRM is not configured.
    """
    if not is_configured():
        return []
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(CLEARANCES_QUERY)
            rows = cur.fetchall()
    finally:
        conn.close()
    return [_row_to_crm_ad(r) for r in rows]


def get_ad(ad_crm_id: str) -> CrmAd | None:
    """Read a single CRM ad by EspoCRM record id (read-only, parameterized).

    Returns ``None`` when the CRM is not configured or no row matches.
    ``ad_crm_id`` is bound as a query parameter — never string-interpolated.
    """
    if not is_configured():
        return None
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(GET_AD_QUERY, (ad_crm_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return _row_to_crm_ad(row)
