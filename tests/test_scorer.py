from datetime import date

from confirmed_ctl.db.models import AdPurchase, BankTransaction
from confirmed_ctl.matching.scorer import (
    _score_amount,
    _score_candidate,
    _score_date,
    _score_vendor,
)


def _txn(amount, vendor, txn_date):
    return BankTransaction(total_amount=amount, vendor_name=vendor, txn_date=txn_date)


def _ad(amount, newspaper, charge_date):
    return AdPurchase(
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
