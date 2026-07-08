from datetime import date

import pytest

from confirmed_ctl.config import Config
from confirmed_ctl.crm_client import ALLOWED_WRITE_FIELDS, CrmClient, build_trigger_query
from confirmed_ctl.models import CaseOutcome, GmailResult, PlaidResult
from confirmed_ctl.pipeline import Pipeline


@pytest.fixture
def pipeline():
    return Pipeline(Config(), dry_run=True)


def _plaid_ok():
    return PlaidResult(
        verified=True,
        txn_name="MIAMI HERALD MEDIA CO",
        amount=1368.00,
        settlement_date=date(2026, 3, 10),
    )


def test_full_reconciliation_marks_done(pipeline, sample_case, monkeypatch):
    monkeypatch.setattr(
        pipeline.gmail, "fetch",
        lambda case: GmailResult(found=True, thread_url="https://mail/x", pdf_path="/tmp/x.pdf"),
    )
    monkeypatch.setattr(pipeline.plaid, "verify", lambda case, window_hours=None: _plaid_ok())

    report = pipeline.process_case(sample_case)

    assert report.outcome == CaseOutcome.DONE
    assert report.writes["statacctgcreditnews"] == "Done"
    assert report.writes["urlgmailadconfirm"] == "https://mail/x"
    assert report.writes["datepaidnews"] == "2026-03-10"
    assert "$1368.00" in report.writes["trxstring"]


def test_gmail_only_is_partial(pipeline, sample_case, monkeypatch):
    monkeypatch.setattr(
        pipeline.gmail, "fetch",
        lambda case: GmailResult(found=True, thread_url="https://mail/x", pdf_path=None),
    )
    monkeypatch.setattr(pipeline.plaid, "verify", lambda case, window_hours=None: PlaidResult())

    report = pipeline.process_case(sample_case)

    assert report.outcome == CaseOutcome.PARTIAL
    assert "statacctgcreditnews" not in report.writes
    assert report.writes["urlgmailadconfirm"] == "https://mail/x"


def test_nothing_found_is_skipped(pipeline, sample_case, monkeypatch):
    monkeypatch.setattr(pipeline.gmail, "fetch", lambda case: GmailResult(found=False))
    monkeypatch.setattr(pipeline.plaid, "verify", lambda case, window_hours=None: PlaidResult())

    report = pipeline.process_case(sample_case)

    assert report.outcome == CaseOutcome.SKIPPED
    assert report.writes == {}


def test_amount_mismatch_not_marked_done(pipeline, sample_case, monkeypatch):
    monkeypatch.setattr(
        pipeline.gmail, "fetch",
        lambda case: GmailResult(found=True, thread_url="https://mail/x", pdf_path="/tmp/x.pdf"),
    )
    mismatch = PlaidResult(
        verified=True, txn_name="X", amount=9999.00, settlement_date=date(2026, 3, 10)
    )
    monkeypatch.setattr(pipeline.plaid, "verify", lambda case, window_hours=None: mismatch)

    report = pipeline.process_case(sample_case)

    assert report.outcome != CaseOutcome.DONE
    assert "statacctgcreditnews" not in report.writes
    assert any("manual review" in n for n in report.notes)


def test_build_trigger_query_params():
    cfg = Config().crm
    sql, params = build_trigger_query(cfg)
    assert "statacctgcreditnews IN (%s, %s)" in sql
    assert "trxstring IS NULL" in sql
    assert params == ["Confirmed", "PaymentConfirmed"]


def test_write_rejects_non_allowlisted_fields(sample_case):
    client = CrmClient(Config().crm, dry_run=True)
    with pytest.raises(ValueError):
        client.write_case_fields(sample_case, {"pricenewsreal": "0"})


def test_allowlist_is_exactly_four_fields():
    assert ALLOWED_WRITE_FIELDS == {
        "statacctgcreditnews",
        "urlgmailadconfirm",
        "trxstring",
        "datepaidnews",
    }
