"""Tests for the deterministic source_txn_id helper (idempotency contract)."""

from datetime import date

from confirmed_ctl.ingest.dedup import deterministic_source_txn_id


def test_fitid_passthrough_for_ofx():
    # OFX path: the bank's FITID is returned verbatim, ignoring the natural key.
    txid = deterministic_source_txn_id(
        "export-ofx", date(2026, 6, 17), 425.00, "LA TIMES", fitid="202606170001"
    )
    assert txid == "202606170001"


def test_natural_key_hash_is_deterministic():
    args = ("email-scan", date(2026, 6, 17), 425.00, "LA TIMES ACH", "1234")
    a = deterministic_source_txn_id(*args)
    b = deterministic_source_txn_id(*args)
    assert a == b
    # SHA-256 hex digest
    assert len(a) == 64
    assert a != "202606170001"


def test_amount_normalization_stable():
    # 12.5 / 12.50 / "12.50" must all hash identically.
    base = deterministic_source_txn_id("export-csv", date(2026, 1, 1), 12.5, "x")
    assert base == deterministic_source_txn_id("export-csv", date(2026, 1, 1), 12.50, "x")
    assert base == deterministic_source_txn_id("export-csv", date(2026, 1, 1), "12.50", "x")


def test_description_normalization_stable():
    a = deterministic_source_txn_id("email-scan", date(2026, 1, 1), 10, "  LA  TIMES ")
    b = deterministic_source_txn_id("email-scan", date(2026, 1, 1), 10, "la times")
    assert a == b


def test_different_inputs_differ():
    a = deterministic_source_txn_id("email-scan", date(2026, 1, 1), 10, "a")
    b = deterministic_source_txn_id("email-scan", date(2026, 1, 1), 20, "a")
    assert a != b


def test_never_empty():
    txid = deterministic_source_txn_id("email-scan", None, None, None)
    assert txid  # always populated, never None/empty


# --------------------------------------------------------------------------- #
# export-csv per-row disambiguator (hardening): two genuinely-distinct same-day,
# same-amount CSV rows must NOT collapse to the same natural-key hash.
# --------------------------------------------------------------------------- #
def test_csv_line_index_disambiguates_identical_rows():
    base = dict(
        source="export-csv",
        posted_date=date(2026, 6, 17),
        amount=2000.00,
        description="LA TIMES",
        last4="1234",
    )
    row0 = deterministic_source_txn_id(**base, line_index=0)
    row1 = deterministic_source_txn_id(**base, line_index=1)
    # Same day + amount + merchant but different line index => distinct ids.
    assert row0 != row1
    # Re-ingesting the SAME file (same line order) stays idempotent.
    assert row0 == deterministic_source_txn_id(**base, line_index=0)


def test_csv_balance_disambiguates_identical_rows():
    base = dict(
        source="export-csv",
        posted_date=date(2026, 6, 17),
        amount=2000.00,
        description="LA TIMES",
    )
    a = deterministic_source_txn_id(**base, balance=100.00)
    b = deterministic_source_txn_id(**base, balance=2100.00)
    assert a != b


def test_csv_no_disambiguator_matches_historical_hash():
    # Without a disambiguator the export-csv hash is byte-identical to the
    # historical 5-component natural key (no churn for existing rows).
    args = ("export-csv", date(2026, 6, 17), 2000.00, "LA TIMES", "1234")
    assert deterministic_source_txn_id(*args) == deterministic_source_txn_id(
        *args, line_index=None, balance=None
    )


def test_disambiguator_ignored_for_non_csv_sources():
    # The disambiguator applies to export-csv ONLY: email-scan / export-ofx
    # natural-key hashes are unchanged even if a line_index is passed.
    args = ("email-scan", date(2026, 6, 17), 2000.00, "LA TIMES", "1234")
    assert deterministic_source_txn_id(*args) == deterministic_source_txn_id(
        *args, line_index=5
    )


def test_disambiguator_never_overrides_fitid():
    # OFX FITID passthrough is unaffected by any disambiguator.
    txid = deterministic_source_txn_id(
        "export-ofx", date(2026, 6, 17), 2000.00, "LA TIMES",
        fitid="FIT-1", line_index=3,
    )
    assert txid == "FIT-1"
