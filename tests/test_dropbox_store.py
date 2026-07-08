from datetime import date

from confirmed_ctl.config import DropboxConfig
from confirmed_ctl.dropbox_store import (
    company_slug,
    receipt_dir,
    receipt_filename,
    remote_path,
)


def test_company_slug_basic():
    assert company_slug("Eduexplora International") == "Eduexplora-International"


def test_company_slug_punctuation_collapses():
    assert company_slug("Martorell's Office Group") == "Martorell-s-Office-Group"


def test_company_slug_truncates_to_max_len():
    slug = company_slug("A Very Long Company Name That Exceeds The Limit", max_len=30)
    assert len(slug) <= 30
    assert not slug.endswith("-")


def test_receipt_filename(sample_case):
    assert (
        receipt_filename(sample_case)
        == "Case-10349_Eduexplora-International_IPR00160880_2026-03-02.pdf"
    )


def test_receipt_filename_missing_date(sample_case):
    sample_case.date_invoiced = None
    assert receipt_filename(sample_case).endswith("_unknown-date.pdf")


def test_receipt_dir(sample_case):
    assert (
        receipt_dir(sample_case, "Receipts/Newspapers")
        == "Receipts/Newspapers/2026/2026-03/Miami-Herald"
    )


def test_remote_path_includes_remote_prefix(sample_case):
    cfg = DropboxConfig(remote="dropbox", base_path="Receipts/Newspapers")
    path = remote_path(sample_case, cfg)
    assert path.startswith("dropbox:Receipts/Newspapers/2026/2026-03/Miami-Herald/")
    assert path.endswith(".pdf")


def test_receipt_dir_uses_invoice_month():
    from confirmed_ctl.models import Case

    case = Case(
        id="x",
        case_number="1",
        company="Foo",
        ad_number="IPR1",
        invoice_amount=1.0,
        date_invoiced=date(2025, 12, 31),
        payment_status="Confirmed",
        news_id="n",
        newspaper_name="Bar",
        newspaper_short="Bar",
    )
    assert receipt_dir(case, "Receipts/Newspapers") == "Receipts/Newspapers/2025/2025-12/Bar"
