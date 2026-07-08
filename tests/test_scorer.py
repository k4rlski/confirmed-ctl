from datetime import date

from confirmed_ctl.db.models import BankTransaction, CrmAd
from confirmed_ctl.matching.scorer import (
    CC_FEE_MULTIPLIER,
    _score_amount,
    _score_amount_ccfee,
    _score_candidate,
    _score_date,
    _score_vendor,
    get_candidate_transactions,
)


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
