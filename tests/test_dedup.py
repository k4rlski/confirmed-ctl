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
