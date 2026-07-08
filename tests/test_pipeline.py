from datetime import date

import pytest

from confirmed_ctl.config import Config, CrmConfig
from confirmed_ctl.crm_client import (
    ALLOWED_WRITE_FIELDS,
    CrmClient,
    build_trigger_query,
    row_to_case,
)
from confirmed_ctl.dropbox_store import receipt_filename
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


def test_trigger_query_selects_case_number_column():
    cfg = CrmConfig(case_number_column="number")
    sql, _ = build_trigger_query(cfg)
    assert "p.number   AS case_number" in sql


def test_trigger_query_rejects_bad_case_number_column():
    cfg = CrmConfig(case_number_column="number; DROP TABLE p")
    with pytest.raises(ValueError):
        build_trigger_query(cfg)


def test_row_to_case_uses_case_number_alias():
    row = {
        "id": "abc-guid",
        "case_number": "10349",
        "company": "Eduexplora International",
        "ad_number": "IPR00160880",
        "invoice_amount": 1368.0,
        "date_invoiced": date(2026, 3, 2),
        "payment_status": "PaymentConfirmed",
        "news_id": "n001",
        "newspaper_name": "Miami Herald",
        "newspaper_short": "Miami-Herald",
    }
    case = row_to_case(row)
    assert case.case_number == "10349"
    assert case.id == "abc-guid"
    assert receipt_filename(case) == (
        "Case-10349_Eduexplora-International_IPR00160880_2026-03-02.pdf"
    )


def test_row_to_case_falls_back_to_id_without_case_number():
    row = {
        "id": "abc-guid",
        "case_number": None,
        "company": "Foo",
        "ad_number": "IPR1",
        "invoice_amount": 1.0,
        "date_invoiced": None,
        "payment_status": "Confirmed",
        "news_id": "n",
        "newspaper_name": "Bar",
        "newspaper_short": "Bar",
    }
    case = row_to_case(row)
    assert case.case_number == "abc-guid"


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
