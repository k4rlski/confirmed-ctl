"""Ad-rep Gmail scan (seed ``ad_reps`` from ad-confirmation From headers).

This adapter reads (READ-ONLY) the impersonated mailbox (``GMAIL_IMPERSONATE``,
default ``karl@perm-ads.com`` — the SAME service account / mailbox the BofA
email-scan and the ad-confirmation thread search already use) and harvests the
``From`` header of ad-confirmation traffic into the ``ad_reps`` registry.

WHICH MAILBOX / QUERY (the finite newspaper-ad-confirmation universe)
---------------------------------------------------------------------
The ad-confirmation universe is small: newspaper ad reps and Paul's ``info@``
forwards that confirm a placed ad. There is no single header that marks them, so
the scan is defined by EXCLUSION rather than a fragile positive filter:

- Base query: everything in the mailbox in the lookback window EXCEPT the BofA
  transaction-alert sender (``-from:onlinebanking@ealerts.bankofamerica.com``).
  Override the whole query via ``settings.AD_REP_SCAN_QUERY`` or the CLI/API
  ``query`` argument to narrow the universe (e.g. a label or a set of
  ``from:`` clauses) once the real sender set is known.
- Per-message From harvest then DROPS internal / non-rep senders by domain
  (``SKIP_DOMAINS`` — the perm-ads.com family and the bank), so only EXTERNAL
  ad-rep addresses are upserted.

NON-DESTRUCTIVE / REVIEW-FIRST
------------------------------
- Upserts ``ad_reps`` only (email unique; ``display_name`` parsed from the From
  header). It NEVER auto-creates rep<->merchant-string links and NEVER touches
  the CRM — a human links reps to bank strings in the MARS vendor-map UI. The
  ``linked_proposed`` count is therefore always ``0`` in this generation (the
  HIGH-bar auto-propose is intentionally deferred; a rep's From domain does not
  reliably map to a bank merchant string like ``DALLAS MORNING NEWS-AD``).
- Gmail access is ``gmail.readonly`` via the shared service client; Gmail
  settings/filters are never modified. ``includeSpamTrash=True`` is inherited
  from :func:`confirmed_ctl.gmail.client.search_messages`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .. import settings
from ..vendors import parse_email_header

log = logging.getLogger("confirmed-ctl.rep-scan")

# The BofA transaction-alert sender — excluded from the rep universe by default
# (its emails are bank alerts, never ad-rep confirmations).
BOFA_SENDER = "onlinebanking@ealerts.bankofamerica.com"

# Internal / non-rep sender domains dropped from the From harvest. These are the
# perm-ads.com family (our own forwards) and the bank. Extend via the
# ``AD_REP_SKIP_DOMAINS`` env (comma-separated) without a code change.
_BASE_SKIP_DOMAINS = {
    "perm-ads.com",
    "bankofamerica.com",
    "ealerts.bankofamerica.com",
    "mail.bankofamerica.com",
}


@dataclass
class ParsedRep:
    """One ad-rep identity parsed from a message From header."""

    email: str
    display_name: str
    domain: str


def skip_domains() -> set[str]:
    """Return the effective skip-domain set (base + env + impersonate domain)."""
    domains = set(_BASE_SKIP_DOMAINS)
    extra = settings.AD_REP_SKIP_DOMAINS
    for d in (extra or "").split(","):
        d = d.strip().lower()
        if d:
            domains.add(d)
    # The impersonated mailbox's own domain is internal by definition.
    imp = (settings.GMAIL_IMPERSONATE or "").strip().lower()
    if "@" in imp:
        domains.add(imp.split("@", 1)[1])
    return domains


def build_query(
    lookback_days: int, base_query: str | None = None, today: date | None = None
) -> str:
    """Build the date-bounded ad-rep scan query.

    ``base_query`` defaults to ``settings.AD_REP_SCAN_QUERY`` (which itself
    defaults to excluding the BofA sender). ``after:`` uses epoch SECONDS
    (unambiguous across time zones), mirroring the BofA email-scan builder.
    """
    base = base_query if base_query is not None else settings.AD_REP_SCAN_QUERY
    base = (base or f"-from:{BOFA_SENDER}").strip()
    today = today or datetime.now(timezone.utc).date()
    after = today - timedelta(days=max(0, lookback_days))
    epoch = int(
        datetime(after.year, after.month, after.day, tzinfo=timezone.utc).timestamp()
    )
    return f"{base} after:{epoch}".strip()


def scan_rep_messages(
    service,
    lookback_days: int,
    query: str | None = None,
    today: date | None = None,
) -> list[ParsedRep]:
    """Search + fetch From headers + parse; return DISTINCT external ad-reps.

    Pure of DB access (so harvesting can be unit-tested offline). Internal /
    bank senders are dropped by :func:`skip_domains`; duplicates collapse on the
    normalized email.
    """
    from ..gmail import client as gmail_client

    q = build_query(lookback_days, query, today)
    log.info("rep-scan query: %s", q)
    skip = skip_domains()

    seen: set[str] = set()
    reps: list[ParsedRep] = []
    for stub in gmail_client.search_messages(service, q):
        msg_id = stub["id"]
        # metadata format is enough — we only need the From header.
        message = gmail_client.get_message(service, msg_id, fmt="metadata")
        headers = gmail_client.get_headers(message)
        from_raw = headers.get("from", "")
        display, email, domain = parse_email_header(from_raw)
        if not email or "@" not in email:
            continue
        if domain in skip:
            continue
        if email in seen:
            continue
        seen.add(email)
        reps.append(ParsedRep(email=email, display_name=display, domain=domain))
    return reps


def run_rep_scan(
    session,
    lookback_days: int | None = None,
    service=None,
    query: str | None = None,
) -> dict:
    """Run a full ad-rep Gmail scan and upsert ``ad_reps``. Returns a counts dict.

    Args:
        session: an open SQLAlchemy session (caller owns commit/close — this
            function commits at the end on success).
        lookback_days: window; defaults to ``settings.AD_REP_SCAN_LOOKBACK_DAYS``.
        service: an optional pre-built Gmail service (tests inject a fake); when
            ``None`` a real service-account service is built lazily.
        query: optional Gmail query override (else the exclude-BofA default).

    Returns ``{"source": "rep-scan", "lookback_days", "found", "upserted",
    "created", "existing", "linked_proposed"}``. ``found`` is the count of
    distinct external rep emails discovered; ``created``/``existing`` split the
    upserts; ``upserted`` = created + existing; ``linked_proposed`` is always 0
    (review-first — links are made by a human in the UI).
    """
    from ..db.models import SyncLog
    from ..vendors import upsert_ad_rep

    lookback_days = (
        settings.AD_REP_SCAN_LOOKBACK_DAYS if lookback_days is None else lookback_days
    )
    started = time.monotonic()
    errors: str | None = None
    found = created = existing = 0

    try:
        if service is None:
            from ..gmail.client import get_gmail_service

            service = get_gmail_service()
        reps = scan_rep_messages(service, lookback_days, query=query)
        found = len(reps)
        for rep in reps:
            _, was_created = upsert_ad_rep(
                session,
                email=rep.email,
                display_name=(rep.display_name or None),
            )
            if was_created:
                created += 1
            else:
                existing += 1
    except Exception as exc:  # record failure in the sync log, then re-raise
        errors = f"{type(exc).__name__}: {exc}"
        log.exception("rep-scan run failed")

    duration_ms = int((time.monotonic() - started) * 1000)
    session.add(
        SyncLog(
            source="rep-scan",
            lookback_days=lookback_days,
            txns_fetched=found,
            txns_new=created,
            txns_updated=existing,
            errors=errors,
            duration_ms=duration_ms,
        )
    )
    session.commit()

    if errors:
        raise RuntimeError(errors)

    return {
        "source": "rep-scan",
        "lookback_days": lookback_days,
        "found": found,
        "upserted": created + existing,
        "created": created,
        "existing": existing,
        "linked_proposed": 0,
    }
