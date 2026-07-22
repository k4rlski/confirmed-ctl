"""confirmed_ctl/crm/client.py

Adapter into the MariaDB CRM ``permtrak2_crm`` on ``permtrak.com``. Ad / case
data lives ONLY in the CRM (see ``confirmed_ctl/db/models.py``); the read side of
this module reads it into lightweight
:class:`~confirmed_ctl.db.models.CrmAd` read views.

Connection pattern mirrors the mars-status reports helper: ``pymysql.connect``
with a ``DictCursor``, ``connect_timeout=10`` and ``read_timeout=60``, driven by
the ``CRM_DB_*`` settings.

CRM WRITE-BACK (the ONLY non-SELECT this package ever issues to the CRM) lives in
:func:`update_ad_clearance`. It is guarded two ways so a write can never happen
by accident:

1. **Feature gate** — it refuses (raises :class:`CrmWriteDisabled`) unless
   ``settings.CRM_WRITE_ENABLED`` is true (env ``CONFIRMED_CTL_CRM_WRITE``,
   default false; set true ONLY on fang).
2. **Strict 3-field allowlist** — it issues a SINGLE parameterized UPDATE whose
   column list is HARDCODED to exactly ``statclearancenews`` (bound to the
   literal ``'["Done"]'``), ``trxstring`` and ``urlgmailadconfirm``, keyed by
   ``WHERE id=%s``. There is no dynamic / caller-supplied column name, ever, and
   every value is bound via ``%s`` (never string-interpolated). The
   staff-owned ``datepaidnews`` column is intentionally NOT written.
"""

from __future__ import annotations

import json

from .. import settings
from ..db.models import CrmAd


class CrmWriteDisabled(RuntimeError):
    """Raised by :func:`update_ad_clearance` when the CRM write gate is off.

    Callers (``/confirm``) catch this to report ``crm_write: "disabled"`` — it
    guarantees NO UPDATE was issued to the live CRM.
    """


class CrmWriteError(RuntimeError):
    """Raised by :func:`update_ad_clearance` when the write cannot be trusted.

    Two cases:

    - **No matching row** — the UPDATE committed but ``cursor.rowcount`` was 0,
      meaning NO ``t_e_s_t_p_e_r_m`` row matched ``ad_crm_id`` (bad/stale id). We
      open the write connection with ``CLIENT.FOUND_ROWS`` so rowcount reflects
      MATCHED (not CHANGED) rows — a re-write of identical values still counts as
      1. rowcount==0 therefore unambiguously means "no such id", never "value
      unchanged".
    - **Invalid input** — an empty/``None`` ``ad_crm_id`` (this write is the last
      line of defense; we never issue an UPDATE without a real id).

    Callers (``/confirm``) map this to a 502 and MUST NOT commit the local
    Postgres confirmation, so the confirm stays cleanly retryable.
    """

# ---------------------------------------------------------------------------
# SQL — the ABCF-X clearances query is reused VERBATIM. The SELECT column list
# and FROM/JOIN are shared so ``get_ad`` returns the exact same shape as a single
# clearances row (only the WHERE differs).
# ---------------------------------------------------------------------------
_SELECT_FROM = """SELECT news.owner AS owner, t_e_s_t_p_e_r_m.id, t_e_s_t_p_e_r_m.adsapproveddate,
       t_e_s_t_p_e_r_m.datebuynews, t_e_s_t_p_e_r_m.datenewsstart, t_e_s_t_p_e_r_m.name,
       t_e_s_t_p_e_r_m.jobtitle, t_e_s_t_p_e_r_m.attyname,
       t_e_s_t_p_e_r_m.beneficiaryfirst, t_e_s_t_p_e_r_m.beneficiarylast,
       t_e_s_t_p_e_r_m.entity, t_e_s_t_p_e_r_m.statclearancenews, t_e_s_t_p_e_r_m.statnews,
       t_e_s_t_p_e_r_m.statacctgcreditnews, t_e_s_t_p_e_r_m.dboxemailthreadcase,
       t_e_s_t_p_e_r_m.adnumbernews, news.name AS newspapers_name, news.rank,
       t_e_s_t_p_e_r_m.pricenewsreal, t_e_s_t_p_e_r_m.casenumber,
       t_e_s_t_p_e_r_m.jobsitestate, t_e_s_t_p_e_r_m.datenewsend
FROM t_e_s_t_p_e_r_m
JOIN news ON t_e_s_t_p_e_r_m.news_id = news.id"""

# Grace window (days) for COMPLETED (statnews ``["Done"]``) clearance ads. A
# newspaper run is over once its statnews flips Active -> Done, but the ad must
# stay visible for reconciliation for a while after the run ends. A Done ad is
# kept in the clearances / unconfirmed feed only while its run END date
# (``datenewsend``) is within this many days (or has no end date); Active ads are
# NEVER bounded. Tune here — keep it bounded so ancient Done ads do not flood the
# reconciliation queue.
DONE_GRACE_DAYS = 90

# The ABCF-X clearances WHERE clause. EspoCRM stores enum fields as JSON string
# arrays, so we match those exact string forms. statnews is now Active OR Done
# (previously Active-only, which dropped a clearance the moment its run ended and
# statnews flipped to Done — even while it was still unmatched/unconfirmed). Done
# rows are bounded by the DONE_GRACE_DAYS window on datenewsend.
_CLEARANCES_WHERE = f"""WHERE statnews IN ('["Active"]', '["Done"]') AND (entity='JKT' OR entity='PA')
  AND statclearancenews='["Confirmed"]' AND t_e_s_t_p_e_r_m.deleted=0
  AND t_e_s_t_p_e_r_m.statpermcase='["Active Case"]'
  AND (statnews='["Active"]'
       OR datenewsend IS NULL
       OR datenewsend >= DATE_SUB(CURDATE(), INTERVAL {DONE_GRACE_DAYS} DAY))
ORDER BY datebuynews DESC"""

CLEARANCES_QUERY = f"{_SELECT_FROM}\n{_CLEARANCES_WHERE}"

# The RECONCILED WHERE clause is IDENTICAL to the clearances WHERE except it
# matches ``statclearancenews='["Done"]'`` (ads this tool has already marked
# Done via the write-back) instead of ``'["Confirmed"]'``. statnews is Active OR
# Done: a real reconcile must REMAIN listed after the newspaper run ends (statnews
# flips to Done) — the old Active-only gate wrongly hid completed reconciles. No
# grace window here: tool-confirmed reconciles are evidence and should persist.
_RECONCILED_WHERE = """WHERE statnews IN ('["Active"]', '["Done"]') AND (entity='JKT' OR entity='PA')
  AND statclearancenews='["Done"]' AND t_e_s_t_p_e_r_m.deleted=0
  AND t_e_s_t_p_e_r_m.statpermcase='["Active Case"]'
ORDER BY datebuynews DESC"""

RECONCILED_QUERY = f"{_SELECT_FROM}\n{_RECONCILED_WHERE}"

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


def _connect_write():
    """Open the CRM connection used by :func:`update_ad_clearance`.

    Identical to :func:`_connect` EXCEPT it sets ``client_flag=CLIENT.FOUND_ROWS``
    so ``cursor.rowcount`` after the UPDATE reflects MATCHED rows rather than
    CHANGED rows. This is critical for idempotent re-writes: without FOUND_ROWS,
    re-writing the SAME values (backfill/retry) returns rowcount=0 and would look
    like a failure. With it, rowcount==1 whenever the id matches (even if values
    are unchanged) and rowcount==0 ONLY when no row matches the id.
    """
    import pymysql
    from pymysql.constants import CLIENT

    return pymysql.connect(
        host=settings.CRM_DB_HOST,
        port=settings.CRM_DB_PORT,
        user=settings.CRM_DB_USER,
        password=settings.CRM_DB_PASS,
        database=settings.CRM_DB_NAME,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=60,
        client_flag=CLIENT.FOUND_ROWS,
    )


def _row_to_crm_ad(row: dict) -> CrmAd:
    """Map one CRM result row (DictCursor) to a :class:`CrmAd` read view."""
    # CRM ``adnumbernews`` carries a trailing space; strip it so downstream
    # consumers (Gmail search / deep-links) get a clean ad number.
    ad_number = row.get("adnumbernews")
    if ad_number is not None:
        ad_number = ad_number.strip()
    return CrmAd(
        crm_id=str(row["id"]) if row.get("id") is not None else None,
        ad_number=ad_number,
        client_name=row.get("name"),
        newspaper_name=row.get("newspapers_name"),
        run_date=row.get("datenewsstart"),
        # Charge date is the "date buy news"; fall back to the run start date.
        expected_charge_date=row.get("datebuynews") or row.get("datenewsstart"),
        expected_amount=row.get("pricenewsreal"),
        case_number=row.get("casenumber"),
        state=row.get("jobsitestate"),
        attorney=row.get("attyname"),
        entity=row.get("entity"),
        job_title=row.get("jobtitle"),
        run_end=row.get("datenewsend"),
        # statnews is a raw EspoCRM enum string (e.g. '["Active"]') — pass
        # through as-is; do NOT parse it here.
        status_news=row.get("statnews"),
        owner=row.get("owner"),
        # Additional ABCF-X contract columns (already selected in _SELECT_FROM).
        approved_date=row.get("adsapproveddate"),
        # buy_date is datebuynews surfaced distinctly from expected_charge_date.
        buy_date=row.get("datebuynews"),
        beneficiary_first=row.get("beneficiaryfirst"),
        beneficiary_last=row.get("beneficiarylast"),
        # clearance_status is the raw EspoCRM statclearancenews enum string
        # (e.g. '["Confirmed"]') — pass through as-is; do NOT parse it here.
        clearance_status=row.get("statclearancenews"),
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


def list_reconciled() -> list[CrmAd]:
    """Return every reconciled (``statclearancenews='["Done"]'``) active JKT/PA
    ad as a :class:`CrmAd`.

    Runs the reconciled query verbatim (read-only) — identical to
    :func:`list_clearances` except it matches the Done clearance status. Returns
    an empty list when the CRM is not configured.
    """
    if not is_configured():
        return []
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(RECONCILED_QUERY)
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


# ---------------------------------------------------------------------------
# WRITE-BACK — the ONLY non-SELECT statement in this package.
# ---------------------------------------------------------------------------

# The clearance-done marker. EspoCRM stores multi-enum fields as JSON arrays, so
# statclearancenews is the literal string '["Done"]' (json.dumps(["Done"])) —
# NOT the plain 'Done'.
CLEARANCE_DONE = json.dumps(["Done"])  # '["Done"]'

# HARDCODED, strict 3-field allowlist. The column list is fixed here and never
# derived from caller input; only the VALUES are bound (``%s``). Order of bound
# params: statclearancenews, trxstring, urlgmailadconfirm, id. The staff-owned
# ``datepaidnews`` column is intentionally NOT part of this write.
UPDATE_AD_CLEARANCE_SQL = (
    "UPDATE t_e_s_t_p_e_r_m "
    "SET statclearancenews=%s, trxstring=%s, urlgmailadconfirm=%s "
    "WHERE id=%s"
)


def update_ad_clearance(
    ad_crm_id: str,
    trxstring: str,
    urlgmailadconfirm: str,
) -> None:
    """Write the clearance-done marker + audit fields back to ONE CRM ad.

    Issues a SINGLE parameterized UPDATE against ``t_e_s_t_p_e_r_m`` setting the
    strict 3-field allowlist (``statclearancenews='["Done"]'``, ``trxstring``,
    ``urlgmailadconfirm``) ``WHERE id=%s``, then commits. Every value is bound via
    ``%s``; no value is ever string-interpolated and no column name is
    caller-supplied. The staff-owned ``datepaidnews`` column is intentionally NOT
    written.

    GATED: if ``settings.CRM_WRITE_ENABLED`` is false this raises
    :class:`CrmWriteDisabled` BEFORE opening any connection, so dev/test never
    touches the live CRM.

    VALIDATED: an empty/``None`` ``ad_crm_id`` raises :class:`CrmWriteError`
    BEFORE any connection is opened — this write is the last line of defense and
    must never issue an UPDATE without a real id.

    VERIFIED: the write connection is opened with ``CLIENT.FOUND_ROWS`` and after
    commit ``cursor.rowcount`` is checked. rowcount==0 means NO row matched
    ``ad_crm_id`` (the id is bad/stale) — this raises :class:`CrmWriteError`
    WITHOUT reporting success, so the caller does not persist a confirmation for a
    write that never landed. rowcount>=1 (a match, even with unchanged values) is
    success.
    """
    if not settings.CRM_WRITE_ENABLED:
        raise CrmWriteDisabled(
            "CRM write-back is disabled: set CONFIRMED_CTL_CRM_WRITE=true "
            "(fang only) to enable update_ad_clearance."
        )

    if not ad_crm_id:
        raise CrmWriteError(
            "update_ad_clearance requires a non-empty ad_crm_id; refusing to "
            "issue an UPDATE without a target CRM record id."
        )

    conn = _connect_write()
    try:
        with conn.cursor() as cur:
            cur.execute(
                UPDATE_AD_CLEARANCE_SQL,
                (
                    CLEARANCE_DONE,
                    trxstring,
                    urlgmailadconfirm,
                    ad_crm_id,
                ),
            )
            matched = cur.rowcount
        conn.commit()
    finally:
        conn.close()

    # FOUND_ROWS => rowcount reflects MATCHED rows. 0 means no CRM row has this
    # id, so the write never landed — surface it instead of a false success.
    if matched == 0:
        raise CrmWriteError(
            f"CRM write matched no row for ad_crm_id={ad_crm_id!r}; the id does "
            "not exist in t_e_s_t_p_e_r_m (nothing was updated)."
        )
