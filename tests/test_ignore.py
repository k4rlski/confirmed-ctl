"""Tests for DB-tracked ignore-strings (Phase 2).

Covers, with no Postgres:

- case-insensitive substring flagging of ingest rows (``apply_ignore_flags`` /
  ``match_ignore_pattern``) and wiring into the email-scan insert path,
- the scorer excluding ``ignored=true`` rows from candidates AND from the
  excluded/near-miss diagnostic (WHERE-clause introspection),
- CLI-backing helpers: ``add_ignore_pattern`` idempotency, ``seed_default_patterns``
  idempotency, ``list`` ordering, and the ``backfill_ignored`` sweep.

A tiny in-memory fake session emulates only the ORM primitives these paths use
and honors the ``active`` / ``ignored`` / ``pattern`` predicates the real DB
applies, so idempotency is exercised for real.
"""

from datetime import date

from confirmed_ctl import settings
from confirmed_ctl.db.models import BankTransaction, CrmAd, IgnoreMemoPattern
from confirmed_ctl.ingest.email_scan import insert_transactions
from confirmed_ctl.ingest.ignore import (
    DEFAULT_IGNORE_PATTERNS,
    add_ignore_pattern,
    apply_ignore_flags,
    backfill_ignored,
    load_active_ignore_patterns,
    match_ignore_pattern,
    seed_default_patterns,
)
from confirmed_ctl.matching.scorer import (
    get_candidate_transactions,
    get_excluded_transactions,
)


# --------------------------------------------------------------------------- #
# In-memory fake session (honors active / ignored / pattern predicates)
# --------------------------------------------------------------------------- #
class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self._preds = []

    def filter(self, *crit):
        self._preds.extend(crit)
        return self

    def filter_by(self, **kw):
        self._preds.append(("_by", kw))
        return self

    def order_by(self, *args):
        return self

    def _apply(self):
        rows = list(self._rows)
        for c in self._preds:
            if isinstance(c, tuple) and c[0] == "_by":
                for k, v in c[1].items():
                    rows = [r for r in rows if getattr(r, k, None) == v]
                continue
            key = getattr(getattr(c, "left", None), "key", None)
            if key == "ignored":
                rows = [r for r in rows if not getattr(r, "ignored", False)]
            elif key == "active":
                rows = [r for r in rows if getattr(r, "active", True)]
            elif key == "pattern":
                val = getattr(getattr(c, "right", None), "value", None)
                rows = [r for r in rows if getattr(r, "pattern", None) == val]
        return rows

    def all(self):
        return self._apply()

    def first(self):
        rows = self._apply()
        return rows[0] if rows else None


class FakeSession:
    def __init__(self):
        self.patterns: list[IgnoreMemoPattern] = []
        self.txns: list[BankTransaction] = []
        self._next_id = 1

    def query(self, model, *args, **kwargs):
        if model is IgnoreMemoPattern:
            return _FakeQuery(self.patterns)
        return _FakeQuery(self.txns)

    def add(self, obj):
        if isinstance(obj, IgnoreMemoPattern):
            if obj.id is None:
                obj.id = self._next_id
                self._next_id += 1
            if obj.active is None:
                obj.active = True
            self.patterns.append(obj)
        elif isinstance(obj, BankTransaction):
            self.txns.append(obj)

    def flush(self):
        pass

    def commit(self):
        pass

    def begin_nested(self):
        return _FakeSavepoint()


class _FakeSavepoint:
    def commit(self):
        pass

    def rollback(self):
        pass


# --------------------------------------------------------------------------- #
# Substring flagging (case-insensitive)
# --------------------------------------------------------------------------- #
def test_match_ignore_pattern_case_insensitive():
    patterns = [("THEAGOGE", "The Agoge (SAAS)")]
    # Lowercase memo with trailing phone/location noise still matches.
    assert match_ignore_pattern(
        ["theagoge llc  +14155551234 ca"], patterns
    ) == ("THEAGOGE", "The Agoge (SAAS)")
    # Mixed case.
    assert match_ignore_pattern(["Payment to TheAgoge"], patterns) is not None
    # No match.
    assert match_ignore_pattern(["LOS ANGELES TIMES"], patterns) is None
    # Empty text.
    assert match_ignore_pattern([], patterns) is None


def test_apply_ignore_flags_sets_ignored_and_reason():
    row = BankTransaction(vendor_name="FIREWORKS.AI 000-0000 CA")
    match = apply_ignore_flags(row, [("FIREWORKS.AI", "Fireworks AI (SAAS)")])
    assert match == ("FIREWORKS.AI", "Fireworks AI (SAAS)")
    assert row.ignored is True
    assert row.ignore_reason == "ignore_pattern:FIREWORKS.AI"


def test_apply_ignore_flags_asterisk_pattern_is_literal_substring():
    # Non-regex substring: the asterisk in "INTUIT *QBOOKS" matches verbatim.
    row = BankTransaction(vendor_name="INTUIT *QBOOKS ONLINE 800-000-0000")
    match = apply_ignore_flags(row, [("INTUIT *QBOOKS", "Intuit QuickBooks Online (SAAS)")])
    assert match is not None
    assert row.ignored is True


def test_apply_ignore_flags_no_match_leaves_row_unflagged():
    row = BankTransaction(vendor_name="SA EXPRESS NEWS ADV -SAN ANTONIO ,TX")
    assert apply_ignore_flags(row, [("THEAGOGE", None)]) is None
    # ignored stays falsy (None in-memory -> server default false on flush).
    assert not row.ignored


def test_apply_ignore_flags_checks_multiple_text_fields():
    # Match lives in private_note, not vendor_name.
    row = BankTransaction(vendor_name="UNKNOWN", private_note="charge THEAGOGE monthly")
    assert apply_ignore_flags(row, [("THEAGOGE", None)]) is not None
    assert row.ignored is True


def test_insert_transactions_flags_saas_rows():
    # The email-scan insert path flags a stored row when a pattern matches, but
    # still stores it (flag, don't drop).
    from confirmed_ctl.ingest.email_scan import EmailTxn

    session = FakeSession()
    saas = EmailTxn(
        posted_date=date(2026, 7, 1),
        amount=-9.0,
        merchant="FIREWORKS.AI 555 CA",
        last4="0353",
        txn_type=None,
        message_id="msgSAAS",
        schema="SCHEMA-CARD",
    )
    normal = EmailTxn(
        posted_date=date(2026, 7, 1),
        amount=-425.0,
        merchant="LOS ANGELES TIMES ACH",
        last4="0353",
        txn_type=None,
        message_id="msgNEWS",
        schema="SCHEMA-CARD",
    )
    patterns = [("FIREWORKS.AI", "Fireworks AI (SAAS)")]
    inserted, skipped = insert_transactions(session, [saas, normal], ignore_patterns=patterns)
    assert (inserted, skipped) == (2, 0)  # both stored
    stored = {t.raw_json["message_id"]: t for t in session.txns}
    assert stored["msgSAAS"].ignored is True
    assert stored["msgSAAS"].ignore_reason == "ignore_pattern:FIREWORKS.AI"
    assert not stored["msgNEWS"].ignored


# --------------------------------------------------------------------------- #
# Scorer exclusion (WHERE clause introspection)
# --------------------------------------------------------------------------- #
class _RecordingQuery:
    def __init__(self, rows):
        self._rows = rows
        self.filters = []

    def filter(self, *criteria):
        self.filters.extend(criteria)
        return self

    def order_by(self, *args):
        return self

    def all(self):
        return list(self._rows)


class _RecordingSession:
    def __init__(self, rows):
        self._rows = rows
        self.last_query = None

    def query(self, *args, **kwargs):
        self.last_query = _RecordingQuery(self._rows)
        return self.last_query


def _has_ignored_false_filter(query):
    for crit in query.filters:
        if getattr(getattr(crit, "left", None), "key", None) == "ignored":
            return True
    return False


def _ad(amount, newspaper, charge_date):
    return CrmAd(
        expected_amount=amount,
        newspaper_name=newspaper,
        expected_charge_date=charge_date,
        run_date=charge_date,
    )


def test_scorer_candidates_exclude_ignored(monkeypatch):
    monkeypatch.setattr(settings, "MATCH_LOOKBACK_DAYS", 10)
    monkeypatch.setattr(settings, "MATCH_LOOKAHEAD_DAYS", 10)
    session = _RecordingSession(rows=[])
    get_candidate_transactions(session, _ad(100.0, "Los Angeles Times", date(2026, 6, 17)))
    assert _has_ignored_false_filter(session.last_query)


def test_scorer_excluded_diagnostic_excludes_ignored(monkeypatch):
    monkeypatch.setattr(settings, "MATCH_LOOKBACK_DAYS", 10)
    monkeypatch.setattr(settings, "MATCH_LOOKAHEAD_DAYS", 10)
    session = _RecordingSession(rows=[])
    get_excluded_transactions(session, _ad(2000.0, "Los Angeles Times", date(2026, 6, 17)))
    assert _has_ignored_false_filter(session.last_query)


# --------------------------------------------------------------------------- #
# CLI-backing helpers: add / list / seed idempotency + backfill
# --------------------------------------------------------------------------- #
def test_add_ignore_pattern_idempotent():
    session = FakeSession()
    row1, created1 = add_ignore_pattern(session, "THEAGOGE", "The Agoge (SAAS)")
    assert created1 is True
    assert row1.pattern == "THEAGOGE"
    # Second add of the same pattern is a no-op.
    row2, created2 = add_ignore_pattern(session, "THEAGOGE", "different label")
    assert created2 is False
    assert row2.id == row1.id
    assert len(session.patterns) == 1
    # Label of the existing row is preserved (not clobbered).
    assert row2.label == "The Agoge (SAAS)"


def test_seed_default_patterns_idempotent():
    session = FakeSession()
    first = seed_default_patterns(session)
    assert first["inserted"] == len(DEFAULT_IGNORE_PATTERNS)
    assert first["existing"] == 0
    # Re-seeding inserts nothing.
    second = seed_default_patterns(session)
    assert second["inserted"] == 0
    assert second["existing"] == len(DEFAULT_IGNORE_PATTERNS)
    assert len(session.patterns) == len(DEFAULT_IGNORE_PATTERNS)


def test_load_active_ignore_patterns_only_active():
    session = FakeSession()
    seed_default_patterns(session)
    # Deactivate one.
    session.patterns[0].active = False
    loaded = load_active_ignore_patterns(session)
    assert len(loaded) == len(DEFAULT_IGNORE_PATTERNS) - 1
    assert all(p for p, _ in loaded)


def test_load_active_ignore_patterns_defensive_on_bad_session():
    class _Broken:
        def query(self, *a, **k):
            raise RuntimeError("no such table")

    assert load_active_ignore_patterns(_Broken()) == []


def test_backfill_ignored_flags_matching_rows_idempotently():
    session = FakeSession()
    seed_default_patterns(session)
    session.txns = [
        BankTransaction(id=1, source="email-scan", source_txn_id="a",
                        txn_date=date(2026, 7, 1), total_amount=-9.0,
                        vendor_name="FIREWORKS.AI 555 CA", ignored=False),
        BankTransaction(id=2, source="email-scan", source_txn_id="b",
                        txn_date=date(2026, 7, 1), total_amount=-425.0,
                        vendor_name="LOS ANGELES TIMES ACH", ignored=False),
        BankTransaction(id=3, source="email-scan", source_txn_id="c",
                        txn_date=date(2026, 7, 1), total_amount=-5.0,
                        vendor_name="THEAGOGE MEMBERSHIP", ignored=False),
    ]
    flagged = backfill_ignored(session)
    assert flagged == 2  # fireworks + agoge; not the LA Times ad charge
    # Re-running is idempotent (already-flagged rows are excluded by the WHERE).
    assert backfill_ignored(session) == 0
