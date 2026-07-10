"""Offline tests for the in-suite receipt downloader (confirmed_ctl.gmail.receipts).

No live Gmail, no Postgres, no network. Pure detection helpers are tested
directly; the Gmail-backed scan/download is exercised against a tiny fake service
that returns canned thread/attachment payloads.
"""

import base64

import pytest

from confirmed_ctl.gmail import receipts as rc


# --------------------------------------------------------------------------- #
# classify_attachment — receipt vs invoice/proof/tearsheet, PDF gate
# --------------------------------------------------------------------------- #
def test_accepts_pdf_with_receipt_keyword_in_filename():
    ok, reason = rc.classify_attachment("receipt_1234.pdf", "application/pdf", "", "")
    assert ok is True
    assert reason == "receipt_keyword"


def test_accepts_when_keyword_only_in_subject_or_body():
    ok, _ = rc.classify_attachment("scan001.pdf", "application/pdf", "Your receipt", "")
    assert ok is True
    ok2, _ = rc.classify_attachment(
        "scan001.pdf", "application/pdf", "Ad placed", "Payment received — receipt attached"
    )
    assert ok2 is True


def test_rejects_non_pdf_even_with_keyword():
    ok, reason = rc.classify_attachment("receipt.png", "image/png", "receipt", "receipt")
    assert ok is False
    assert reason == "not_pdf"


def test_rejects_invoice_proof_tearsheet_by_denylist():
    for name in ("invoice_9.pdf", "ad-proof.pdf", "tearsheet.pdf", "tear sheet.pdf"):
        ok, reason = rc.classify_attachment(name, "application/pdf", "receipt", "receipt")
        assert ok is False, name
        assert reason == "denylist", name


def test_denylist_wins_over_receipt_keyword_in_body():
    # "invoice" in the subject beats "receipt" in the body — it's still a bill.
    ok, reason = rc.classify_attachment(
        "document.pdf", "application/pdf", "Your invoice", "receipt receipt receipt"
    )
    assert ok is False
    assert reason == "denylist"


def test_pdf_without_keyword_rejected_in_strict_mode():
    ok, reason = rc.classify_attachment("statement2026.pdf", "application/pdf", "", "")
    # 'statement' is denylisted -> denylist (checked before keyword).
    assert ok is False
    assert reason == "denylist"
    ok2, reason2 = rc.classify_attachment("document.pdf", "application/pdf", "", "")
    assert ok2 is False
    assert reason2 == "no_receipt_keyword"


def test_loose_mode_accepts_any_non_denylisted_pdf():
    ok, reason = rc.classify_attachment(
        "document.pdf", "application/pdf", "", "", require_keyword=False
    )
    assert ok is True
    assert reason == "pdf_not_denylisted"
    # ...but denylist still applies in loose mode.
    ok2, reason2 = rc.classify_attachment(
        "invoice.pdf", "application/pdf", "", "", require_keyword=False
    )
    assert ok2 is False
    assert reason2 == "denylist"


def test_pdf_detected_by_mime_when_extension_missing():
    ok, _ = rc.classify_attachment("receipt-nodot", "application/pdf", "receipt", "")
    assert ok is True


def test_pdf_uppercase_extension_accepted():
    ok, _ = rc.classify_attachment("RECEIPT.PDF", "", "", "")
    # No mime, uppercase .PDF extension still counts as PDF; filename has keyword.
    assert ok is True


# --------------------------------------------------------------------------- #
# _walk_parts — recursive multipart walk
# --------------------------------------------------------------------------- #
def test_walk_parts_finds_nested_attachment():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": ""}},
            {
                "mimeType": "multipart/related",
                "parts": [
                    {
                        "filename": "receipt.pdf",
                        "mimeType": "application/pdf",
                        "body": {"attachmentId": "att-1"},
                    }
                ],
            },
        ],
    }
    parts = list(rc._walk_parts(payload))
    assert len(parts) == 1
    assert parts[0]["filename"] == "receipt.pdf"


def test_walk_parts_ignores_inline_parts_without_attachment_id():
    payload = {
        "parts": [
            {"filename": "", "mimeType": "text/html", "body": {"data": "x"}},
            {"filename": "logo.png", "mimeType": "image/png", "body": {}},  # no attachmentId
        ]
    }
    assert list(rc._walk_parts(payload)) == []


def test_message_text_decodes_nested_text_parts():
    def enc(s):
        return base64.urlsafe_b64encode(s.encode()).decode()

    payload = {
        "parts": [
            {"mimeType": "text/plain", "body": {"data": enc("your receipt is ready")}},
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/html", "body": {"data": enc("<b>paid</b>")}}
                ],
            },
        ]
    }
    text = rc._message_text(payload).lower()
    assert "receipt" in text
    assert "paid" in text


# --------------------------------------------------------------------------- #
# scan_thread / download_thread_receipts against a fake Gmail service
# --------------------------------------------------------------------------- #
def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


class _FakeAttachments:
    def __init__(self, blobs):
        self._blobs = blobs

    def get(self, userId, messageId, id):  # noqa: N803 - Gmail API kwargs
        data = self._blobs[id]

        class _E:
            def execute(_self):
                return {"data": base64.urlsafe_b64encode(data).decode()}

        return _E()


class _FakeMessages:
    def __init__(self, blobs):
        self._att = _FakeAttachments(blobs)

    def attachments(self):
        return self._att


class _FakeThreads:
    def __init__(self, thread):
        self._thread = thread

    def get(self, userId, id, format):  # noqa: A002 - Gmail API kwarg name
        class _E:
            def execute(_self):
                return self._thread

        return _E()


class _FakeUsers:
    def __init__(self, thread, blobs):
        self._threads = _FakeThreads(thread)
        self._messages = _FakeMessages(blobs)

    def threads(self):
        return self._threads

    def messages(self):
        return self._messages


class _FakeService:
    def __init__(self, thread, blobs):
        self._users = _FakeUsers(thread, blobs)

    def users(self):
        return self._users


def _thread_with(parts, subject="Ad confirmation"):
    return {
        "messages": [
            {
                "id": "m1",
                "payload": {
                    "headers": [{"name": "Subject", "value": subject}],
                    "parts": parts,
                },
            }
        ]
    }


def test_scan_thread_classifies_receipt_and_rejects_invoice():
    parts = [
        {"filename": "receipt.pdf", "mimeType": "application/pdf", "body": {"attachmentId": "a1"}},
        {"filename": "invoice.pdf", "mimeType": "application/pdf", "body": {"attachmentId": "a2"}},
    ]
    svc = _FakeService(_thread_with(parts), {"a1": b"%PDF-1", "a2": b"%PDF-2"})
    scan = rc.scan_thread(svc, "t1")
    assert len(scan.hits) == 2
    assert {h.filename for h in scan.accepted} == {"receipt.pdf"}


def test_download_writes_only_receipts_and_dedupes(tmp_path, monkeypatch):
    monkeypatch.setattr(rc.settings, "RECEIPTS_BASE_PATH", str(tmp_path))
    parts = [
        {"filename": "receipt.pdf", "mimeType": "application/pdf", "body": {"attachmentId": "a1"}},
        {"filename": "invoice.pdf", "mimeType": "application/pdf", "body": {"attachmentId": "a2"}},
        {
            "filename": "receipt_dup.pdf",
            "mimeType": "application/pdf",
            "body": {"attachmentId": "a3"},
        },
    ]
    # a1 and a3 are byte-identical -> SHA-256 dedup keeps one.
    svc = _FakeService(
        _thread_with(parts), {"a1": b"SAME-PDF", "a2": b"%PDF-inv", "a3": b"SAME-PDF"}
    )
    r = rc.download_thread_receipts(svc, "t1", "IPR001", "2026", "07")
    assert len(r["saved"]) == 1  # invoice skipped, duplicate skipped
    reasons = {s["reason"] for s in r["skipped"]}
    assert "denylist" in reasons  # invoice.pdf
    assert "duplicate_sha256" in reasons  # receipt_dup.pdf
    saved = tmp_path / "2026" / "07" / "IPR001" / "receipt.pdf"
    assert saved.exists()
    assert saved.read_bytes() == b"SAME-PDF"


def test_download_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(rc.settings, "RECEIPTS_BASE_PATH", str(tmp_path))
    parts = [
        {"filename": "receipt.pdf", "mimeType": "application/pdf", "body": {"attachmentId": "a1"}},
    ]
    svc = _FakeService(_thread_with(parts), {"a1": b"%PDF"})
    r = rc.download_thread_receipts(svc, "t1", "IPR001", "2026", "07", dry_run=True)
    assert r["saved"] == []
    assert not (tmp_path / "2026").exists()


# --------------------------------------------------------------------------- #
# process_pending_receipts — scope + DB update via a fake session
# --------------------------------------------------------------------------- #
class _Conf:
    def __init__(self, ad_crm_id, thread, ad_number, confirmed_at):
        self.ad_crm_id = ad_crm_id
        self.gmail_thread_id = thread
        self.ad_number = ad_number
        self.confirmed_at = confirmed_at
        self.receipt_file_path = None
        self.receipt_url = None


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows):
        self._rows = rows
        self.committed = False

    def query(self, *a):
        return _Query(self._rows)

    def commit(self):
        self.committed = True


def test_process_pending_updates_receipt_path(tmp_path, monkeypatch):
    import datetime

    monkeypatch.setattr(rc.settings, "RECEIPTS_BASE_PATH", str(tmp_path))
    parts = [
        {"filename": "receipt.pdf", "mimeType": "application/pdf", "body": {"attachmentId": "a1"}},
    ]
    svc = _FakeService(_thread_with(parts), {"a1": b"%PDF-real"})
    monkeypatch.setattr(rc, "get_gmail_service", lambda: svc)

    conf = _Conf("crm1", "t1", "IPR001", datetime.datetime(2026, 7, 9))
    session = _Session([conf])
    result = rc.process_pending_receipts(session)
    assert result["processed"] == 1
    assert result["downloaded"] == 1
    assert conf.receipt_file_path.endswith("IPR001/receipt.pdf")
    assert session.committed is True


def test_process_pending_dry_run_no_db_write(tmp_path, monkeypatch):
    import datetime

    monkeypatch.setattr(rc.settings, "RECEIPTS_BASE_PATH", str(tmp_path))
    parts = [
        {"filename": "receipt.pdf", "mimeType": "application/pdf", "body": {"attachmentId": "a1"}},
    ]
    svc = _FakeService(_thread_with(parts), {"a1": b"%PDF-real"})
    monkeypatch.setattr(rc, "get_gmail_service", lambda: svc)

    conf = _Conf("crm1", "t1", "IPR001", datetime.datetime(2026, 7, 9))
    session = _Session([conf])
    result = rc.process_pending_receipts(session, dry_run=True)
    assert conf.receipt_file_path is None
    assert session.committed is False
    assert result["pending"] == 1


@pytest.mark.parametrize("rows", [[]])
def test_process_pending_empty_scope_noop(rows):
    session = _Session(rows)
    result = rc.process_pending_receipts(session)
    assert result["pending"] == 0
    assert result["processed"] == 0
    assert session.committed is False
