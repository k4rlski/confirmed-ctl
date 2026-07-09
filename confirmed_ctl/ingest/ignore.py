"""DB-tracked ignore-string matching for bank-transaction ingest.

Recurring SAAS / vendor charges (software subscriptions, AI APIs, accounting
software, …) are not newspaper-ad payments, so they must never surface as
reconcile candidates. Rather than delete them, every ingest adapter *flags* a
matching row (``bank_transactions.ignored = true`` + ``ignore_reason``) so the
audit trail is preserved and the scorer simply skips it.

Patterns live in the ``ignore_memo_patterns`` table (a SHORT stable substring +
label). Matching is case-insensitive **substring containment** (plain literal,
not regex — so an asterisk such as ``INTUIT *QBOOKS`` matches verbatim) against
every text field on the row (``vendor_name``, ``private_note``, ``account_name``,
``doc_number``, ``line_descriptions`` items).

Adapters load the active patterns ONCE per run (:func:`load_active_ignore_patterns`)
and apply them per row (:func:`apply_ignore_flags`). Loading is defensive: if the
table does not exist yet (pre-migration) or the session is a lightweight test
stand-in, it returns ``[]`` so ingest still works (rows are simply not flagged).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

log = logging.getLogger("confirmed-ctl.ignore")

# The default SAAS/vendor ignore patterns seeded on first run. Each is the
# SHORTEST stable literal substring that reliably identifies the vendor's bank
# memo, so trailing phone / location / date noise does not break the match.
#
# Intuit note: matching is plain (non-regex) substring containment, so the
# asterisk in ``INTUIT *QBOOKS`` is matched verbatim and is NOT a problem — it is
# the stable vendor core Intuit uses on QuickBooks Online charges.
DEFAULT_IGNORE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("THEAGOGE", "The Agoge (SAAS)"),
    ("FIREWORKS.AI", "Fireworks AI (SAAS)"),
    ("INTUIT *QBOOKS", "Intuit QuickBooks Online (SAAS)"),
)


def load_active_ignore_patterns(session) -> list[tuple[str, str | None]]:
    """Return ``[(pattern, label), …]`` for every ACTIVE ignore pattern.

    Defensive: any failure (table missing pre-migration, a lightweight test
    session that does not implement the full query API, etc.) is swallowed and
    an empty list is returned so ingest degrades gracefully (rows just aren't
    flagged) rather than crashing.
    """
    try:
        from ..db.models import IgnoreMemoPattern

        rows = (
            session.query(IgnoreMemoPattern)
            .filter(IgnoreMemoPattern.active.is_(True))
            .all()
        )
        return [(r.pattern, r.label) for r in rows if r.pattern]
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("could not load ignore patterns (skipping flagging): %s", exc)
        return []


def _row_texts(row) -> list[str]:
    """Collect the free-text fields of a ``BankTransaction`` row to match against.

    Covers the payee/memo-ish fields that exist on the model: ``vendor_name``,
    ``private_note``, ``account_name``, ``doc_number`` and each entry of the
    ``line_descriptions`` array. Missing/empty values are skipped.
    """
    texts: list[str] = []
    for attr in ("vendor_name", "private_note", "account_name", "doc_number"):
        value = getattr(row, attr, None)
        if value:
            texts.append(str(value))
    line_descriptions = getattr(row, "line_descriptions", None)
    if line_descriptions:
        for item in line_descriptions:
            if item:
                texts.append(str(item))
    return texts


def match_ignore_pattern(
    texts: Iterable[str], patterns: Iterable[tuple[str, str | None]]
) -> tuple[str, str | None] | None:
    """Return the first ``(pattern, label)`` whose pattern is a case-insensitive
    substring of any of ``texts``; ``None`` when nothing matches."""
    lowered = [t.lower() for t in texts if t]
    if not lowered:
        return None
    for pattern, label in patterns:
        if not pattern:
            continue
        needle = pattern.lower()
        if any(needle in hay for hay in lowered):
            return pattern, label
    return None

def apply_ignore_flags(
    row, patterns: Iterable[tuple[str, str | None]]
) -> tuple[str, str | None] | None:
    """Flag ``row`` in place when its text matches an active ignore pattern.

    Sets ``row.ignored = True`` and ``row.ignore_reason = "ignore_pattern:<pattern>"``
    for the first matching pattern. Returns the matched ``(pattern, label)`` (or
    ``None`` if nothing matched). No-op when ``patterns`` is empty.
    """
    patterns = list(patterns or [])
    if not patterns:
        return None
    match = match_ignore_pattern(_row_texts(row), patterns)
    if match is None:
        return None
    pattern, _label = match
    row.ignored = True
    row.ignore_reason = f"ignore_pattern:{pattern}"
    return match


def add_ignore_pattern(session, pattern: str, label: str | None = None):
    """Idempotently insert an ignore pattern; return ``(model, created: bool)``.

    Idempotency key is the exact ``pattern`` string. If a row already exists it
    is returned unchanged (``created=False``); its ``active``/``label`` are left
    as-is so a manual edit is not clobbered by a re-seed.
    """
    from ..db.models import IgnoreMemoPattern

    pattern = (pattern or "").strip()
    if not pattern:
        raise ValueError("ignore pattern must be a non-empty string")
    existing = (
        session.query(IgnoreMemoPattern)
        .filter(IgnoreMemoPattern.pattern == pattern)
        .first()
    )
    if existing is not None:
        return existing, False
    row = IgnoreMemoPattern(pattern=pattern, label=label, active=True)
    session.add(row)
    session.flush()
    return row, True


def seed_default_patterns(session) -> dict:
    """Idempotently insert the :data:`DEFAULT_IGNORE_PATTERNS`.

    Returns counts ``{"inserted": n, "existing": m}``. Safe to run repeatedly.
    Caller owns the commit.
    """
    inserted = existing = 0
    for pattern, label in DEFAULT_IGNORE_PATTERNS:
        _row, created = add_ignore_pattern(session, pattern, label)
        if created:
            inserted += 1
        else:
            existing += 1
    return {"inserted": inserted, "existing": existing}


def backfill_ignored(session) -> int:
    """Flag existing ``bank_transactions`` rows that match an active pattern.

    One-time (but idempotent) sweep run after seeding: any row whose text matches
    an active ignore pattern and is not already flagged is set ``ignored=true`` +
    ``ignore_reason``. Returns the number of rows newly flagged. Caller owns the
    commit.
    """
    from ..db.models import BankTransaction

    patterns = load_active_ignore_patterns(session)
    if not patterns:
        return 0
    rows = (
        session.query(BankTransaction)
        .filter(BankTransaction.ignored.is_(False))
        .all()
    )
    flagged = 0
    for row in rows:
        if apply_ignore_flags(row, patterns) is not None:
            flagged += 1
    return flagged
