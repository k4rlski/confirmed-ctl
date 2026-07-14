from datetime import date

import pytest

from confirmed_ctl import settings
from confirmed_ctl.db.models import BankTransaction, CrmAd
from confirmed_ctl.matching.scorer import (
    CC_FEE_MULTIPLIER,
    REP_EMAIL_BOOST,
    VENDOR_LINK_BOOST,
    VENDOR_STRING_BOOST,
    _amount_matches_ccfee,
    _score_amount,
    _score_amount_ccfee,
    _score_candidate,
    _score_date,
    _score_vendor,
    get_candidate_transactions,
    get_excluded_transactions,
)
from confirmed_ctl.vendors import VendorLinkIndex


def _txn(amount, vendor, txn_date):
    return BankTransaction(total_amount=amount, vendor_name=vendor, txn_date=txn_date)


def _ad(amount, newspaper, charge_date):
    return CrmAd(
        expected_amount=amount,
        newspaper_name=newspaper,
        expected_charge_date=charge_date,
        run_date=charge_date,
    )


def test_score_amount_buckets():
    assert _score_amount(100.0, 100.0) == 1.0
    assert _score_amount(100.5, 100.0) == 0.90    # within 1%
    assert _score_amount(104.0, 100.0) == 0.60    # within 5%
    assert _score_amount(114.0, 100.0) == 0.30    # within 15%
    assert _score_amount(200.0, 100.0) == 0.0
    assert _score_amount(100.0, 0.0) == 0.0       # guard: expected==0


def test_score_amount_ccfee_exact_and_fee():
    # Exact invoice amount is a perfect match.
    assert _score_amount_ccfee(1368.00, 1368.00) == 1.0
    # Same invoice grossed up by the 3.99% CC fee is an equally strong match.
    fee_amount = 1368.00 * CC_FEE_MULTIPLIER  # 1422.58...
    assert _score_amount_ccfee(fee_amount, 1368.00) >= 0.95
    # Magnitude-based: a debit stored as a negative still matches.
    assert _score_amount_ccfee(-fee_amount, 1368.00) >= 0.95


def test_score_amount_ccfee_within_tolerance():
    # Rounded fee amount within $1 of the fee target still scores strong.
    assert _score_amount_ccfee(1422.58, 1368.00) >= 0.95
    # An unrelated amount is not a match.
    assert _score_amount_ccfee(999.00, 1368.00) == 0.0


def test_score_amount_ccfee_falls_back_to_buckets():
    # 112 is within 15% of 100 but not near 100 or the fee target (103.99), so it
    # falls back to the graduated bucket (0.30) rather than a strong match.
    score = _score_amount_ccfee(112.0, 100.0)
    assert 0.0 < score < 0.95


def test_score_candidate_cc_fee_txn_ranks_strong():
    d = date(2026, 6, 17)
    fee_amount = round(500.00 * CC_FEE_MULTIPLIER, 2)  # 519.95
    txn = _txn(fee_amount, "MIAMI HERALD ACH", d)
    ad = _ad(500.00, "Miami Herald", d)
    # amount(>=0.95)*0.5 + vendor(1.0)*0.3 + date(1.0)*0.2 -> well above 0.9
    assert _score_candidate(txn, ad) >= 0.9


def test_score_vendor_direct_substring():
    assert _score_vendor("LOS ANGELES TIMES ACH", "Los Angeles Times") == 1.0


def test_score_vendor_known_mapping():
    # canonical "los angeles times" -> alias "la times" appears in the bank string
    assert _score_vendor("LA TIMES PYMT 0617", "Los Angeles Times") == 0.90


def test_score_vendor_none_and_fuzzy():
    assert _score_vendor(None, "Los Angeles Times") == 0.0
    assert _score_vendor("Completely Unrelated Vendor", "Los Angeles Times") == 0.0


def test_score_date_buckets():
    d = date(2026, 6, 17)
    assert _score_date(d, d) == 1.0
    assert _score_date(date(2026, 6, 18), d) == 0.85
    assert _score_date(date(2026, 6, 19), d) == 0.65
    assert _score_date(date(2026, 6, 20), d) == 0.40
    assert _score_date(date(2026, 6, 22), d) == 0.20
    assert _score_date(date(2026, 6, 30), d) == 0.0
    assert _score_date(None, d) == 0.0


def test_score_candidate_perfect_match_is_one():
    d = date(2026, 6, 17)
    txn = _txn(425.00, "LOS ANGELES TIMES ACH", d)
    ad = _ad(425.00, "Los Angeles Times", d)
    # 0.50*1 + 0.30*1 + 0.20*1 == 1.0
    assert _score_candidate(txn, ad) == 1.0


def test_score_candidate_amount_mismatch_lowers_score():
    d = date(2026, 6, 17)
    good = _score_candidate(_txn(425.00, "LA TIMES", d), _ad(425.00, "Los Angeles Times", d))
    bad = _score_candidate(_txn(999.00, "LA TIMES", d), _ad(425.00, "Los Angeles Times", d))
    assert bad < good


def test_score_amount_ccfee_none_expected_is_zero():
    # CRM pricenewsreal can be NULL -> expected is None. Must not raise and must
    # contribute no amount score.
    assert _score_amount_ccfee(1368.0, None) == 0.0


def test_score_candidate_none_expected_amount_no_amount_contribution():
    # An ad whose expected_amount is None (CRM pricenewsreal NULL) must score
    # without raising TypeError and yield no amount contribution — only the
    # vendor + date signals count.
    d = date(2026, 6, 17)
    txn = _txn(1368.0, "LOS ANGELES TIMES ACH", d)
    ad = CrmAd(
        expected_amount=None,
        newspaper_name="Los Angeles Times",
        expected_charge_date=d,
        run_date=d,
    )
    score = _score_candidate(txn, ad)
    # vendor(1.0)*0.30 + date(1.0)*0.20 == 0.50, amount contributes 0.
    assert score == pytest.approx(0.50)


def test_get_candidates_returns_empty_when_ad_has_no_dates():
    # An ad with neither expected_charge_date nor run_date has no date anchor;
    # this must degrade gracefully (empty list) rather than raise TypeError.
    ad = CrmAd(
        expected_amount=100.0,
        newspaper_name="Los Angeles Times",
        expected_charge_date=None,
        run_date=None,
    )
    # db is never touched because the guard returns before any query.
    assert get_candidate_transactions(db=None, ad=ad) == []


# --------------------------------------------------------------------------- #
# Configurable candidate window
# --------------------------------------------------------------------------- #
class _RecordingQuery:
    """Captures the filter/order_by criteria; returns the canned rows."""

    def __init__(self, rows):
        self._rows = rows
        self.filters: list = []

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
        self.last_query: _RecordingQuery | None = None

    def query(self, *args, **kwargs):
        self.last_query = _RecordingQuery(self._rows)
        return self.last_query


def _txn_date_bounds(query):
    """Extract the txn_date >= / <= bind values from captured filter criteria."""
    bounds: dict[str, date] = {}
    for crit in query.filters:
        left = getattr(crit, "left", None)
        right = getattr(crit, "right", None)
        op = getattr(getattr(crit, "operator", None), "__name__", None)
        if left is None or right is None or op is None:
            continue
        if getattr(left, "key", None) == "txn_date":
            bounds[op] = right.value
    return bounds


def test_configurable_window_default_from_settings(monkeypatch):
    # With no explicit window, the scorer uses the configurable settings
    # (wider 10/10 defaults) for [charge_date - lookback, charge_date + lookahead].
    monkeypatch.setattr(settings, "MATCH_LOOKBACK_DAYS", 10)
    monkeypatch.setattr(settings, "MATCH_LOOKAHEAD_DAYS", 10)
    session = _RecordingSession(rows=[])
    ad = _ad(100.0, "Los Angeles Times", date(2026, 6, 17))

    get_candidate_transactions(session, ad)

    bounds = _txn_date_bounds(session.last_query)
    assert bounds["ge"] == date(2026, 6, 7)   # charge_date - 10
    assert bounds["le"] == date(2026, 6, 27)  # charge_date + 10


def test_configurable_window_env_override_widens(monkeypatch):
    # A wider env-configured window is honored.
    monkeypatch.setattr(settings, "MATCH_LOOKBACK_DAYS", 14)
    monkeypatch.setattr(settings, "MATCH_LOOKAHEAD_DAYS", 21)
    session = _RecordingSession(rows=[])
    ad = _ad(100.0, "Los Angeles Times", date(2026, 6, 17))

    get_candidate_transactions(session, ad)

    bounds = _txn_date_bounds(session.last_query)
    assert bounds["ge"] == date(2026, 6, 3)    # charge_date - 14
    assert bounds["le"] == date(2026, 7, 8)    # charge_date + 21


def test_explicit_window_args_override_settings(monkeypatch):
    monkeypatch.setattr(settings, "MATCH_LOOKBACK_DAYS", 10)
    monkeypatch.setattr(settings, "MATCH_LOOKAHEAD_DAYS", 10)
    session = _RecordingSession(rows=[])
    ad = _ad(100.0, "Los Angeles Times", date(2026, 6, 17))

    get_candidate_transactions(session, ad, lookback_days=3, lookahead_days=1)

    bounds = _txn_date_bounds(session.last_query)
    assert bounds["ge"] == date(2026, 6, 14)   # charge_date - 3
    assert bounds["le"] == date(2026, 6, 18)   # charge_date + 1


# --------------------------------------------------------------------------- #
# Excluded near-miss reasons
# --------------------------------------------------------------------------- #
def _bank_txn(txn_id, amount, txn_date, *, confirmed=None):
    return BankTransaction(
        id=txn_id,
        source="email-scan",
        source_txn_id=f"tx-{txn_id}",
        txn_date=txn_date,
        total_amount=amount,
        vendor_name="LA TIMES",
        confirmed_ad_crm_id=confirmed,
    )


def test_amount_matches_ccfee_boolean():
    assert _amount_matches_ccfee(2000.0, 2000.0) is True
    # Debit stored negative still matches by magnitude.
    assert _amount_matches_ccfee(-2000.0, 2000.0) is True
    # CC-fee grossed-up amount matches.
    assert _amount_matches_ccfee(2000.0 * CC_FEE_MULTIPLIER, 2000.0) is True
    assert _amount_matches_ccfee(999.0, 2000.0) is False
    assert _amount_matches_ccfee(2000.0, None) is False


def test_excluded_reasons_out_of_window_and_already_matched(monkeypatch):
    monkeypatch.setattr(settings, "MATCH_LOOKBACK_DAYS", 10)
    monkeypatch.setattr(settings, "MATCH_LOOKAHEAD_DAYS", 10)
    charge = date(2026, 6, 17)  # window [06-07, 06-27]
    rows = [
        # Plausible by amount, OUT of window, unmatched -> out_of_window.
        _bank_txn(1, -2000.0, date(2026, 7, 15)),
        # Plausible by amount, IN window, already matched -> already_matched.
        _bank_txn(2, -2000.0, date(2026, 6, 17), confirmed="RECX"),
        # Plausible by amount, IN window, unmatched -> IS a candidate (skipped).
        _bank_txn(3, -2000.0, date(2026, 6, 18)),
        # Amount does NOT match -> skipped entirely.
        _bank_txn(4, -55.0, date(2026, 7, 20)),
    ]
    session = _RecordingSession(rows=rows)
    ad = _ad(2000.0, "Los Angeles Times", charge)

    excluded = get_excluded_transactions(session, ad)

    by_id = {e["txn_id"]: e for e in excluded}
    assert by_id[1]["reason"] == "out_of_window"
    assert by_id[1]["txn_date"] == "2026-07-15"
    assert by_id[2]["reason"] == "already_matched"
    # The in-window unmatched candidate and the amount mismatch are NOT excluded.
    assert 3 not in by_id
    assert 4 not in by_id


def test_excluded_is_bounded(monkeypatch):
    monkeypatch.setattr(settings, "MATCH_LOOKBACK_DAYS", 10)
    monkeypatch.setattr(settings, "MATCH_LOOKAHEAD_DAYS", 10)
    charge = date(2026, 6, 17)
    # 25 plausible out-of-window rows; result must be capped at the limit (10).
    rows = [_bank_txn(i, -2000.0, date(2026, 8, 1)) for i in range(25)]
    session = _RecordingSession(rows=rows)
    ad = _ad(2000.0, "Los Angeles Times", charge)

    excluded = get_excluded_transactions(session, ad)
    assert len(excluded) == 10


def test_excluded_empty_when_no_expected_amount(monkeypatch):
    session = _RecordingSession(rows=[_bank_txn(1, -2000.0, date(2026, 7, 15))])
    ad = _ad(None, "Los Angeles Times", date(2026, 6, 17))
    assert get_excluded_transactions(session, ad) == []


# --------------------------------------------------------------------------- #
# Vendor-link boost (VendorLinkIndex + get_candidate_transactions integration)
# --------------------------------------------------------------------------- #
def test_link_index_match_reasons_and_boosts():
    idx = VendorLinkIndex(
        linked={
            "DALLAS MORNING NEWS-AD-DALLAS ,TX": {
                "rep_ids": [1],
                "rep_emails": ["roshanda.buchanan@mediumgiant.co"],
            }
        },
        catalog={
            "DALLAS MORNING NEWS-AD-DALLAS ,TX",
            "SF CHRONICLE ADVTZNG -SAN FRANCISCO,CA",
        },
    )
    # Linked string, no From match -> vendor_link only.
    reasons, boost = idx.match("Dallas Morning News-AD-Dallas ,TX")
    assert reasons == ["vendor_link"]
    assert boost == pytest.approx(VENDOR_LINK_BOOST)
    # Linked string + matching confirmation From -> rep_email stacks on.
    reasons, boost = idx.match(
        "DALLAS MORNING NEWS-AD-DALLAS ,TX",
        from_emails={"Roshanda.Buchanan@MediumGiant.co"},
    )
    assert reasons == ["vendor_link", "rep_email"]
    assert boost == pytest.approx(VENDOR_LINK_BOOST + REP_EMAIL_BOOST)
    # Catalogued but NOT linked -> weak vendor_string only.
    reasons, boost = idx.match("SF CHRONICLE ADVTZNG -SAN FRANCISCO,CA")
    assert reasons == ["vendor_string"]
    assert boost == pytest.approx(VENDOR_STRING_BOOST)
    # Unknown string -> no reason, no boost (never penalized).
    reasons, boost = idx.match("SOME RANDOM VENDOR")
    assert reasons == [] and boost == 0.0
    # Blank vendor -> no boost.
    assert idx.match(None) == ([], 0.0)


def test_get_candidates_applies_vendor_link_boost():
    d = date(2026, 6, 17)
    # Two candidates with IDENTICAL base signals; only one's string is linked.
    linked_txn = _txn(500.0, "DALLAS MORNING NEWS-AD-DALLAS ,TX", d)
    linked_txn.id = 1
    linked_txn.source = "email-scan"
    linked_txn.source_txn_id = "a"
    plain_txn = _txn(500.0, "SOME OTHER PAPER", d)
    plain_txn.id = 2
    plain_txn.source = "email-scan"
    plain_txn.source_txn_id = "b"
    session = _RecordingSession(rows=[linked_txn, plain_txn])
    ad = _ad(500.0, None, d)  # no newspaper -> vendor base score 0 for both

    idx = VendorLinkIndex(
        linked={"DALLAS MORNING NEWS-AD-DALLAS ,TX": {"rep_ids": [1], "rep_emails": []}},
        catalog={"DALLAS MORNING NEWS-AD-DALLAS ,TX"},
    )
    scored = get_candidate_transactions(session, ad, link_index=idx)
    by_id = {c["transaction"].id: c for c in scored}
    # Linked candidate is lifted by exactly VENDOR_LINK_BOOST above its base.
    assert by_id[1]["match_reasons"] == ["vendor_link"]
    assert by_id[1]["boost_delta"] == pytest.approx(VENDOR_LINK_BOOST)
    assert by_id[1]["score"] == pytest.approx(by_id[1]["base_score"] + VENDOR_LINK_BOOST)
    # Non-linked candidate keeps its base score (NOT penalized), no reasons.
    assert by_id[2]["match_reasons"] == []
    assert by_id[2]["score"] == pytest.approx(by_id[2]["base_score"])
    # The linked candidate now ranks first.
    assert scored[0]["transaction"].id == 1


def test_get_candidates_no_link_index_is_unchanged():
    d = date(2026, 6, 17)
    txn = _txn(425.0, "LOS ANGELES TIMES ACH", d)
    txn.id = 1
    txn.source = "email-scan"
    txn.source_txn_id = "a"
    session = _RecordingSession(rows=[txn])
    ad = _ad(425.0, "Los Angeles Times", d)
    scored = get_candidate_transactions(session, ad)  # no link_index
    assert scored[0]["match_reasons"] == []
    assert scored[0]["boost_delta"] == 0.0
    assert scored[0]["score"] == pytest.approx(1.0)  # unchanged perfect match
