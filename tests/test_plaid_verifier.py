from datetime import date

from confirmed_ctl.plaid_verifier import amount_matches, format_trxstring


def test_format_trxstring():
    result = format_trxstring(date(2026, 3, 10), "MIAMI HERALD MEDIA CO", 1368.00)
    assert result == "2026-03-10 | MIAMI HERALD MEDIA CO | $1368.00"


def test_format_trxstring_rounds_two_decimals():
    assert format_trxstring(date(2026, 1, 1), "X", 928).endswith("$928.00")


def test_amount_matches_within_tolerance():
    assert amount_matches(1368.00, 1368.50, tolerance=1.00)


def test_amount_matches_outside_tolerance():
    assert not amount_matches(1368.00, 1370.00, tolerance=1.00)


def test_amount_matches_exact():
    assert amount_matches(457.00, 457.00, tolerance=0.0)
