"""confirmed_ctl/gmail/receipts.py  (receipt-ctl core logic — confirmed-ctl half)

The confirmed-ctl / ad-buy half of receipt-ctl: given a CONFIRMED ad's Gmail
ad-confirmation thread, find and download the **receipt** PDF(s) attached to that
thread and record the local path on the ``ad_confirmations`` row.

Scope (deliberately narrow):

- Only CONFIRMED ads — ``ad_confirmations`` rows that already have a
  ``gmail_thread_id`` set AND a NULL ``receipt_file_path``. We never scan mail
  for un-reconciled ads.
- Only the AD-CONFIRMATION thread (``ad_confirmations.gmail_thread_id``) — NEVER
  the BofA transaction-alert thread (that lives on ``bank_transactions`` and is a
  different email entirely).

Detection (receipts, NOT invoices/proofs/tearsheets):

- Attachment must be a PDF (``application/pdf`` mime OR ``.pdf``/``.PDF`` name).
- A denylist rejects invoices / ad-proofs / tearsheets / estimates / statements
  by filename or subject even if the body mentions "receipt".
- Default (strict) mode also requires the keyword ``receipt`` in the filename,
  subject, or body. ``require_keyword=False`` loosens to "any non-denylisted PDF"
  — the grab-all mitigation for threads whose receipt PDF is not named/worded
  "receipt" (use only after a strict dry-run yields 0 hits on a known thread).

Safety:

- Gmail access is READ-ONLY (``gmail.readonly`` via the shared service client).
- Multipart bodies are walked RECURSIVELY (BofA/vendor mail nests attachments
  below ``multipart/*`` wrappers, which the old top-level-only walk missed).
- Files are de-duplicated by SHA-256 content hash within an ad's receipt dir, so
  re-running never writes a second copy of the same PDF.

The standalone ``receipt-ctl`` tool (the vendor-portal scraper in
``core-v5/receipt-ctl``) is a SEPARATE, later effort for the vendor-ctl half;
this module is the in-suite confirmed-ctl downloader only.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import settings
from .client import get_gmail_service

log = logging.getLogger(__name__)

# Keyword that marks a document as a receipt (strict mode). Lower-cased,
# letter-boundary match (see _text_has_word) so ``receipt_1234.pdf`` matches but
# ``unpaid`` does NOT satisfy ``paid``.
RECEIPT_KEYWORDS = ("receipt", "receipts", "paid", "payment received")

# Filename/subject markers that are NEVER receipts — reject even if "receipt"
# appears elsewhere. Ad proofs and tearsheets are ad EVIDENCE, not payment proof;
# invoices/estimates/statements are bills, not receipts.
DENY_KEYWORDS = (
    "invoice",
    "proof",
    "tearsheet",
    "tear sheet",
    "tear-sheet",
    "estimate",
    "quote",
    "statement",
    "insertion order",
)


@dataclass
class AttachmentHit:
    """One attachment considered for download (accepted or not)."""

    message_id: str
    filename: str
    mime_type: str
    attachment_id: str
    accepted: bool
    reason: str


@dataclass
class ThreadScan:
    """Result of scanning one ad-confirmation thread (no download)."""

    thread_id: str
    hits: list[AttachmentHit] = field(default_factory=list)

    @property
    def accepted(self) -> list[AttachmentHit]:
        return [h for h in self.hits if h.accepted]


# --------------------------------------------------------------------------- #
# Detection helpers (pure — unit-tested without Gmail)
# --------------------------------------------------------------------------- #
def _is_pdf(filename: str | None, mime_type: str | None) -> bool:
    if mime_type and "pdf" in mime_type.lower():
        return True
    return bool(filename) and filename.lower().endswith(".pdf")


def _text_has_any(text: str | None, keywords: tuple[str, ...]) -> bool:
    """Substring match (used for the denylist — intentionally aggressive)."""
    t = (text or "").lower()
    return any(k in t for k in keywords)


def _text_has_word(text: str | None, keywords: tuple[str, ...]) -> bool:
    """Letter-boundary match (used for receipt keywords).

    The keyword must not be flanked by ASCII letters, so ``unpaid`` does NOT
    satisfy ``paid`` while filename separators (``receipt_1234.pdf``,
    ``receipt-2.pdf``, ``receipt 2024``) still match. Digits/underscores/hyphens
    count as boundaries (unlike ``\\b``, which treats ``_`` as a word char).
    """
    t = (text or "").lower()
    return any(
        re.search(r"(?<![a-z])" + re.escape(k) + r"(?![a-z])", t) for k in keywords
    )


def classify_attachment(
    filename: str | None,
    mime_type: str | None,
    subject: str | None,
    body: str | None,
    *,
    require_keyword: bool = True,
) -> tuple[bool, str]:
    """Decide whether one attachment is a receipt to download.

    Returns ``(accepted, reason)``. Order matters:
    1. Non-PDFs are rejected.
    2. A denylisted FILENAME (``invoice.pdf``) is always rejected — the filename
       is the strongest per-attachment signal, so an "invoice receipt.pdf" is
       still rejected as an invoice.
    3. A receipt-keyword FILENAME (``receipt.pdf``) is always accepted, even when
       the message subject mentions an invoice/proof (a common mixed thread).
    4. Only THEN does the message subject/body denylist apply — so a generically
       named PDF sitting in an invoice/proof thread is rejected by context.
    """
    if not _is_pdf(filename, mime_type):
        return False, "not_pdf"
    fname = filename or ""
    # (2) Filename denylist wins over everything (invoice.pdf, proof.pdf, …).
    if _text_has_any(fname, DENY_KEYWORDS):
        return False, "denylist"
    # (3) A receipt-named file is accepted regardless of thread context.
    if _text_has_word(fname, RECEIPT_KEYWORDS):
        return True, "receipt_keyword"
    # (4) Otherwise the surrounding subject/body can still disqualify the file.
    if _text_has_any(subject or "", DENY_KEYWORDS):
        return False, "denylist_context"
    if not require_keyword:
        return True, "pdf_not_denylisted"
    if _text_has_word(f"{subject or ''} {body or ''}", RECEIPT_KEYWORDS):
        return True, "receipt_keyword"
    return False, "no_receipt_keyword"


def _walk_parts(payload: dict):
    """Yield every MIME part with a filename + attachmentId, RECURSIVELY.

    Parts are yielded in document order (depth-first) so downloads/dedup are
    deterministic regardless of nesting depth.
    """
    part = payload or {}
    if part.get("filename") and part.get("body", {}).get("attachmentId"):
        yield part
    for sub in part.get("parts", []) or []:
        yield from _walk_parts(sub)


def _message_subject(message: dict) -> str:
    for h in message.get("payload", {}).get("headers", []) or []:
        if h.get("name", "").lower() == "subject":
            return h.get("value", "") or ""
    return ""


def _message_text(payload: dict) -> str:
    """Concatenate decoded text/* part bodies (for keyword scanning only)."""
    texts: list[str] = []
    stack = [payload or {}]
    while stack:
        part = stack.pop()
        for sub in part.get("parts", []) or []:
            stack.append(sub)
        if str(part.get("mimeType", "")).startswith("text/"):
            data = part.get("body", {}).get("data")
            if data:
                try:
                    texts.append(base64.urlsafe_b64decode(data).decode("utf-8", "ignore"))
                except Exception:  # noqa: BLE001 - best-effort keyword scan only
                    pass
    return " ".join(texts)


# --------------------------------------------------------------------------- #
# Gmail-backed scan / download
# --------------------------------------------------------------------------- #
def scan_thread(service, thread_id: str, *, require_keyword: bool = True) -> ThreadScan:
    """Classify every attachment in an ad-confirmation thread (NO download)."""
    thread = (
        service.users()
        .threads()
        .get(userId="me", id=thread_id, format="full")
        .execute()
    )
    scan = ThreadScan(thread_id=thread_id)
    for message in thread.get("messages", []):
        payload = message.get("payload", {})
        subject = _message_subject(message)
        body = _message_text(payload)
        for part in _walk_parts(payload):
            accepted, reason = classify_attachment(
                part.get("filename"),
                part.get("mimeType"),
                subject,
                body,
                require_keyword=require_keyword,
            )
            scan.hits.append(
                AttachmentHit(
                    message_id=message["id"],
                    filename=part.get("filename") or "",
                    mime_type=part.get("mimeType") or "",
                    attachment_id=part["body"]["attachmentId"],
                    accepted=accepted,
                    reason=reason,
                )
            )
    return scan


def _existing_hashes(save_dir: Path) -> dict[str, str]:
    """Map sha256 -> path for PDFs already in ``save_dir`` (dedup baseline)."""
    hashes: dict[str, str] = {}
    if not save_dir.exists():
        return hashes
    for p in save_dir.iterdir():
        if p.is_file():
            try:
                hashes[hashlib.sha256(p.read_bytes()).hexdigest()] = str(p)
            except OSError:
                continue
    return hashes


def _dedupe_name(save_dir: Path, filename: str) -> Path:
    """Return a non-colliding path (append -1, -2, … before the extension)."""
    target = save_dir / filename
    if not target.exists():
        return target
    stem, dot, ext = filename.rpartition(".")
    stem = stem or filename
    i = 1
    while True:
        candidate = save_dir / (f"{stem}-{i}.{ext}" if dot else f"{filename}-{i}")
        if not candidate.exists():
            return candidate
        i += 1


def download_thread_receipts(
    service,
    thread_id: str,
    ad_number: str,
    year: str,
    month: str,
    *,
    require_keyword: bool = True,
    dry_run: bool = False,
) -> dict:
    """Download the accepted receipt PDFs from one thread. SHA-256 de-duplicated.

    Returns ``{saved, present, would_download, skipped, scanned}``:
    - ``saved``: paths newly written to disk this run.
    - ``present``: paths of accepted receipts that already existed on disk (same
      SHA-256) — so a crashed prior run that wrote the file but never committed
      the DB can still be reconciled to a ``receipt_file_path``.
    - ``would_download``: filenames accepted in ``dry_run`` (nothing written).
    - ``skipped``: ``[{filename, reason}]`` for rejected/duplicate attachments.
    """
    scan = scan_thread(service, thread_id, require_keyword=require_keyword)
    result: dict = {
        "saved": [],
        "present": [],
        "would_download": [],
        "skipped": [],
        "scanned": len(scan.hits),
    }

    for hit in scan.hits:
        if not hit.accepted:
            result["skipped"].append({"filename": hit.filename, "reason": hit.reason})

    if dry_run:
        result["would_download"] = [h.filename for h in scan.accepted]
        return result

    save_dir = Path(settings.RECEIPTS_BASE_PATH) / year / month / ad_number
    save_dir.mkdir(parents=True, exist_ok=True)
    seen = _existing_hashes(save_dir)

    for hit in scan.accepted:
        attachment = (
            service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=hit.message_id, id=hit.attachment_id)
            .execute()
        )
        file_data = base64.urlsafe_b64decode(attachment["data"])
        digest = hashlib.sha256(file_data).hexdigest()
        if digest in seen:
            # Already on disk (dedup). Record the existing path so the DB can
            # still be updated even when nothing new was written this run.
            result["present"].append(seen[digest])
            result["skipped"].append(
                {"filename": hit.filename, "reason": "duplicate_sha256"}
            )
            continue
        filename = hit.filename or f"receipt_{hit.message_id}.pdf"
        file_path = _dedupe_name(save_dir, filename)
        with open(file_path, "wb") as f:
            f.write(file_data)
        seen[digest] = str(file_path)
        result["saved"].append(str(file_path))

    return result


def process_pending_receipts(
    db_session,
    *,
    ad_crm_id: str | None = None,
    require_keyword: bool = True,
    dry_run: bool = False,
) -> dict:
    """Download receipts for confirmed ads missing a ``receipt_file_path``.

    Scope: ``ad_confirmations`` with ``gmail_thread_id`` set AND
    ``receipt_file_path`` NULL (optionally narrowed to one ``ad_crm_id``). Uses
    the AD-CONFIRMATION thread only. On success writes ``receipt_file_path`` (the
    first PDF) and ``receipt_url`` (comma-joined when multiple). ``dry_run``
    reports classifications without downloading or writing the DB.
    """
    from ..db.models import AdConfirmation

    q = db_session.query(AdConfirmation).filter(
        AdConfirmation.gmail_thread_id.isnot(None),
        AdConfirmation.receipt_file_path.is_(None),
    )
    if ad_crm_id:
        q = q.filter(AdConfirmation.ad_crm_id == ad_crm_id)
    pending = q.all()

    results: dict = {
        "pending": len(pending),
        "processed": 0,
        "downloaded": 0,
        "would_download": 0,
        "skipped": 0,
        "errors": [],
        "details": [],
    }
    if not pending:
        return results

    service = get_gmail_service()
    for conf in pending:
        try:
            confirmed_date = conf.confirmed_at
            year = str(confirmed_date.year) if confirmed_date else "unknown"
            month = f"{confirmed_date.month:02d}" if confirmed_date else "00"
            r = download_thread_receipts(
                service,
                thread_id=conf.gmail_thread_id,
                # Ad is referenced logically; prefer the human ad number.
                ad_number=conf.ad_number or conf.ad_crm_id,
                year=year,
                month=month,
                require_keyword=require_keyword,
                dry_run=dry_run,
            )
            results["details"].append(
                {"ad_crm_id": conf.ad_crm_id, "ad_number": conf.ad_number, **r}
            )
            saved = r["saved"]
            present = r.get("present", [])
            results["downloaded"] += len(saved)
            results["would_download"] += len(r.get("would_download", []))
            results["skipped"] += len(r["skipped"])
            # On-disk receipts for this ad = newly saved + pre-existing (dedup).
            # Setting the path from ``present`` too recovers ads whose file was
            # written by a prior run that crashed before the DB commit.
            on_disk = saved + present
            if on_disk and not dry_run:
                conf.receipt_file_path = on_disk[0]
                # Always mirror the on-disk path(s) into receipt_url (schema/RAG
                # contract), comma-joined when a thread yields multiple PDFs.
                conf.receipt_url = ",".join(on_disk)
                results["processed"] += 1
        except Exception as e:  # noqa: BLE001 - per-ad isolation; keep going
            log.exception("receipt download failed for ad_crm_id=%s", conf.ad_crm_id)
            results["errors"].append(f"{conf.ad_crm_id}: {e}")

    if not dry_run:
        db_session.commit()
    return results
