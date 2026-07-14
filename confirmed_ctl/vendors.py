"""Ad-rep <-> bank merchant-string registry service helpers.

Pure(ish) data-layer helpers shared by the ``/confirmed-ctl/vendor-map`` API
routes and the ``vendors scan`` CLI. All state lives in the standalone
``confirmed_ctl`` Postgres (tables ``ad_reps`` / ``bank_merchant_strings`` /
``ad_rep_merchant_links``) — NEVER the CRM.

Normalization is deliberately simple and explicit so the same raw string always
maps to one catalog row:

- ``normalize_merchant_string`` — uppercase + collapse internal whitespace. The
  embedded ``-CITY ,ST`` / phone tail is kept verbatim (it distinguishes two
  otherwise-identical merchants), so ``DALLAS MORNING NEWS-AD-DALLAS ,TX`` and a
  short ``DALLAS MORNING NEWS`` are DIFFERENT catalog rows on purpose — the
  operator links whichever variants belong to a rep.
- ``normalize_email`` — trim + lowercase; the display name is parsed off a
  ``Name <email>`` header separately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .db.models import (
    AdRep,
    AdRepMerchantLink,
    BankMerchantString,
    BankTransaction,
)

_WS_RE = re.compile(r"\s+")
# Grabs the address inside a "Display Name <addr@dom>" header, else the bare addr.
_ADDR_RE = re.compile(r"<([^>]+)>")


def normalize_merchant_string(raw: str | None) -> str:
    """Uppercase + collapse internal whitespace; strip ends. '' for blank."""
    if not raw:
        return ""
    return _WS_RE.sub(" ", str(raw)).strip().upper()


def normalize_email(raw: str | None) -> str:
    """Lower-case + trim a bare email address. '' for blank."""
    if not raw:
        return ""
    return str(raw).strip().lower()


def parse_email_header(raw: str | None) -> tuple[str, str, str]:
    """Split a ``Name <addr@dom>`` (or bare ``addr``) header.

    Returns ``(display_name, email_lower, domain_lower)``. When no angle-bracket
    address is present the whole trimmed value is treated as the email. The
    display name is '' when the header is a bare address.
    """
    if not raw:
        return "", "", ""
    s = str(raw).strip()
    m = _ADDR_RE.search(s)
    if m:
        email = normalize_email(m.group(1))
        display = s[: m.start()].strip().strip('"').strip()
    else:
        email = normalize_email(s)
        display = ""
    domain = email.split("@", 1)[1] if "@" in email else ""
    return display, email, domain


# --------------------------------------------------------------------------- #
# Upserts (idempotent by natural key)
# --------------------------------------------------------------------------- #
def upsert_ad_rep(
    db: Session,
    *,
    email: str,
    display_name: str | None = None,
    org: str | None = None,
    notes: str | None = None,
) -> tuple[AdRep, bool]:
    """Get-or-create an ad-rep by normalized email. Returns ``(row, created)``.

    On an existing row, non-empty ``display_name``/``org``/``notes`` fill blanks
    (never clobber an operator-entered value with a blank). ``domain`` is derived
    from the email. Caller commits.
    """
    norm = normalize_email(email)
    if not norm:
        raise ValueError("email is required")
    domain = norm.split("@", 1)[1] if "@" in norm else None
    row = db.query(AdRep).filter(AdRep.email == norm).first()
    if row is None:
        row = AdRep(
            email=norm,
            display_name=(display_name or None),
            org=(org or None),
            domain=domain,
            notes=(notes or None),
        )
        db.add(row)
        db.flush()
        return row, True
    if display_name and not row.display_name:
        row.display_name = display_name
    if org and not row.org:
        row.org = org
    if notes and not row.notes:
        row.notes = notes
    if domain and not row.domain:
        row.domain = domain
    return row, False


def upsert_merchant_string(
    db: Session,
    *,
    raw_string: str,
    source: str = "manual",
    notes: str | None = None,
) -> tuple[BankMerchantString, bool]:
    """Get-or-create a bank merchant string by normalized key.

    Returns ``(row, created)``. On an existing row the raw spelling is appended
    to ``raw_examples`` (deduped) and ``last_seen`` is bumped. Caller commits.
    """
    norm = normalize_merchant_string(raw_string)
    if not norm:
        raise ValueError("normalized_string is empty")
    now = datetime.now(timezone.utc)
    raw = (raw_string or "").strip()
    row = (
        db.query(BankMerchantString)
        .filter(BankMerchantString.normalized_string == norm)
        .first()
    )
    if row is None:
        row = BankMerchantString(
            normalized_string=norm,
            raw_examples=[raw] if raw else [],
            source=source if source in ("manual", "scan", "bofa_alert") else "manual",
            notes=(notes or None),
            first_seen=now,
            last_seen=now,
        )
        db.add(row)
        db.flush()
        return row, True
    examples = list(row.raw_examples or [])
    if raw and raw not in examples:
        examples.append(raw)
        row.raw_examples = examples
    row.last_seen = now
    if notes and not row.notes:
        row.notes = notes
    return row, False


def link_rep_to_string(
    db: Session,
    *,
    ad_rep_id: int,
    bank_merchant_string_id: int,
    confidence: str = "manual",
    created_by: str | None = None,
    notes: str | None = None,
) -> tuple[AdRepMerchantLink, bool]:
    """Get-or-create a rep<->string link (unique pair). Returns ``(row, created)``."""
    existing = (
        db.query(AdRepMerchantLink)
        .filter(
            AdRepMerchantLink.ad_rep_id == ad_rep_id,
            AdRepMerchantLink.bank_merchant_string_id == bank_merchant_string_id,
        )
        .first()
    )
    if existing is not None:
        return existing, False
    row = AdRepMerchantLink(
        ad_rep_id=ad_rep_id,
        bank_merchant_string_id=bank_merchant_string_id,
        confidence=confidence or "manual",
        created_by=created_by,
        notes=(notes or None),
    )
    db.add(row)
    db.flush()
    return row, True


# --------------------------------------------------------------------------- #
# Serializers
# --------------------------------------------------------------------------- #
def rep_to_dict(rep: AdRep) -> dict:
    return {
        "id": rep.id,
        "email": rep.email,
        "display_name": rep.display_name,
        "org": rep.org,
        "domain": rep.domain,
        "notes": rep.notes,
        "active": bool(rep.active),
        "created_at": rep.created_at.isoformat() if rep.created_at else None,
        "updated_at": rep.updated_at.isoformat() if rep.updated_at else None,
    }


def string_to_dict(s: BankMerchantString) -> dict:
    return {
        "id": s.id,
        "normalized_string": s.normalized_string,
        "raw_examples": list(s.raw_examples or []),
        "source": s.source,
        "notes": s.notes,
        "active": bool(s.active),
        "first_seen": s.first_seen.isoformat() if s.first_seen else None,
        "last_seen": s.last_seen.isoformat() if s.last_seen else None,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


def link_to_dict(link: AdRepMerchantLink) -> dict:
    """Serialize a link joined to its rep + string (both eager-loadable)."""
    rep = link.ad_rep
    s = link.merchant_string
    return {
        "id": link.id,
        "ad_rep_id": link.ad_rep_id,
        "bank_merchant_string_id": link.bank_merchant_string_id,
        "confidence": link.confidence,
        "created_by": link.created_by,
        "notes": link.notes,
        "created_at": link.created_at.isoformat() if link.created_at else None,
        "ad_rep_email": rep.email if rep else None,
        "ad_rep_display_name": rep.display_name if rep else None,
        "ad_rep_org": rep.org if rep else None,
        "normalized_string": s.normalized_string if s else None,
        "raw_examples": list(s.raw_examples or []) if s else [],
        "source": s.source if s else None,
    }


# --------------------------------------------------------------------------- #
# Link index (read-only view consumed by the scorer's vendor-link boost)
# --------------------------------------------------------------------------- #
@dataclass
class VendorLinkIndex:
    """A cheap, read-once view of the rep<->merchant-string registry.

    Built once per request by :func:`build_vendor_link_index` and passed into
    the scorer so ranking can prefer bank transactions whose merchant string is
    a KNOWN newspaper-ad vendor (catalogued) — and even more so when that string
    is LINKED to an ad-rep (and, best of all, when that rep is the sender of the
    ad's own confirmation email). It never mutates state.

    - ``linked`` maps a ``normalized_string`` -> ``{"rep_ids": [...],
      "rep_emails": [...]}`` for strings that have at least one rep link.
    - ``catalog`` is the set of ALL catalogued ``normalized_string`` values
      (linked or not) — a hit here alone is a weak "this looks like a real ad
      vendor" signal.

    Both keys are normalized via :func:`normalize_merchant_string`, so callers
    look up ``normalize_merchant_string(txn.vendor_name)`` directly.
    """

    linked: dict[str, dict] = field(default_factory=dict)
    catalog: set[str] = field(default_factory=set)

    def match(
        self, vendor_name: str | None, from_emails: set[str] | None = None
    ) -> tuple[list[str], float]:
        """Return ``(match_reasons, boost_delta)`` for one bank vendor string.

        Reasons (most-specific first) and their additive boosts:

        - ``vendor_link`` (+VENDOR_LINK_BOOST): the string is linked to a rep.
        - ``rep_email``   (+REP_EMAIL_BOOST): additionally, one of the ad's
          confirmation-email From addresses matches a rep linked to this string
          (the rep-email path — the strongest, ad-specific signal).
        - ``vendor_string`` (+VENDOR_STRING_BOOST): the string is catalogued but
          NOT linked to any rep (weak "known ad vendor" signal).

        Boosts are ADDITIVE to the base score and the caller clamps the total to
        1.0. A non-catalogued string yields ``([], 0.0)`` so unlinked candidates
        are never penalized — only linked/known ones are lifted.
        """
        # Imported lazily to avoid any import ordering concerns; the constants
        # live in the scorer next to the base weights.
        from .matching.scorer import (
            REP_EMAIL_BOOST,
            VENDOR_LINK_BOOST,
            VENDOR_STRING_BOOST,
        )

        norm = normalize_merchant_string(vendor_name)
        if not norm:
            return [], 0.0
        info = self.linked.get(norm)
        if info:
            reasons = ["vendor_link"]
            boost = VENDOR_LINK_BOOST
            if from_emails:
                rep_emails = {normalize_email(e) for e in info.get("rep_emails", [])}
                if rep_emails & {normalize_email(e) for e in from_emails}:
                    reasons.append("rep_email")
                    boost += REP_EMAIL_BOOST
            return reasons, boost
        if norm in self.catalog:
            return ["vendor_string"], VENDOR_STRING_BOOST
        return [], 0.0


def build_vendor_link_index(db: Session) -> VendorLinkIndex:
    """Build a :class:`VendorLinkIndex` from the registry tables (read-only).

    Two small queries: all catalogued ``bank_merchant_strings`` (active only),
    and all ``ad_rep_merchant_links`` joined to their rep. Cheap enough to build
    once per ``/candidates`` or ``/suggested`` request.
    """
    index = VendorLinkIndex()
    for s in db.query(BankMerchantString).filter(
        BankMerchantString.active.is_(True)
    ):
        if s.normalized_string:
            index.catalog.add(s.normalized_string)

    for link in db.query(AdRepMerchantLink).all():
        s = link.merchant_string
        rep = link.ad_rep
        if s is None or not s.normalized_string:
            continue
        entry = index.linked.setdefault(
            s.normalized_string, {"rep_ids": [], "rep_emails": []}
        )
        entry["rep_ids"].append(link.ad_rep_id)
        if rep is not None and rep.email:
            entry["rep_emails"].append(rep.email)
    return index


# --------------------------------------------------------------------------- #
# Scan / seed (non-destructive)
# --------------------------------------------------------------------------- #
def scan_seed_merchant_strings(
    db: Session, *, lookback_days: int | None = None
) -> dict:
    """Seed ``bank_merchant_strings`` from distinct ``bank_transactions.vendor_name``.

    Non-destructive upsert (source ``scan``): pulls distinct non-ignored bank
    ``vendor_name`` values (optionally within a ``lookback_days`` window on
    ``txn_date``) and upserts each as a catalog row. Then computes the set of
    catalog strings that are NOT yet linked to any rep (the "propose unlinked"
    list). It NEVER auto-creates reps or links — high-volume auto-linking is out
    of scope; the operator reviews and links in the UI. Caller commits.

    Returns a counts dict: ``{scanned, created, existing, unlinked_count}``.
    """
    from datetime import date, timedelta

    q = db.query(BankTransaction.vendor_name).filter(
        BankTransaction.vendor_name.isnot(None),
        BankTransaction.ignored.is_(False),
    )
    if lookback_days is not None and lookback_days > 0:
        cutoff = date.today() - timedelta(days=lookback_days)
        q = q.filter(BankTransaction.txn_date >= cutoff)

    seen: set[str] = set()
    created = existing = 0
    for (vendor_name,) in q.all():
        norm = normalize_merchant_string(vendor_name)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        _, was_created = upsert_merchant_string(
            db, raw_string=vendor_name, source="scan"
        )
        if was_created:
            created += 1
        else:
            existing += 1

    linked_ids = {
        row[0] for row in db.query(AdRepMerchantLink.bank_merchant_string_id).all()
    }
    unlinked = (
        db.query(BankMerchantString)
        .filter(~BankMerchantString.id.in_(linked_ids) if linked_ids else True)
        .count()
    )

    return {
        "scanned": len(seen),
        "created": created,
        "existing": existing,
        "unlinked_count": unlinked,
    }
