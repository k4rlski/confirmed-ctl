"""confirmed_ctl/gmail/receipts.py  (receipt-ctl core logic)

Given a Gmail thread ID, download all attachments (PDFs, images)
and save them to the local receipts directory.
Update the ad_confirmations record with the file path.

Note: this is the lightweight in-suite downloader. The standalone ``receipt-ctl``
tool (docs/RECEIPT-CTL.md) supersedes this with dedup, cloud storage, and its own
audit tables; this stays here for the ``confirmed-ctl receipts`` convenience path.
"""

from __future__ import annotations

import base64
from pathlib import Path

from .. import settings
from .client import get_gmail_service


def download_receipt(
    thread_id: str,
    ad_number: str,
    year: str,
    month: str,
) -> list[str]:
    """
    Download all attachments from a Gmail thread.
    Returns list of saved file paths.
    """
    service = get_gmail_service()
    thread = service.users().threads().get(
        userId="me", id=thread_id, format="full"
    ).execute()

    save_dir = Path(settings.RECEIPTS_BASE_PATH) / year / month / ad_number
    save_dir.mkdir(parents=True, exist_ok=True)

    saved_paths = []

    for message in thread.get("messages", []):
        parts = message.get("payload", {}).get("parts", [])
        for part in parts:
            if part.get("filename") and part.get("body", {}).get("attachmentId"):
                attachment = service.users().messages().attachments().get(
                    userId="me",
                    messageId=message["id"],
                    id=part["body"]["attachmentId"],
                ).execute()

                file_data = base64.urlsafe_b64decode(attachment["data"])
                file_name = part["filename"] or f"receipt_{message['id']}.pdf"
                file_path = save_dir / file_name

                with open(file_path, "wb") as f:
                    f.write(file_data)

                saved_paths.append(str(file_path))

    return saved_paths


def process_pending_receipts(db_session) -> dict:
    """
    Batch job: find all confirmed ads with a gmail_thread_id but no receipt_file_path.
    Download receipts for each. Called by cron or CLI.
    """
    from ..db.models import AdConfirmation

    pending = (
        db_session.query(AdConfirmation)
        .filter(
            AdConfirmation.gmail_thread_id.isnot(None),
            AdConfirmation.receipt_file_path.is_(None),
        )
        .all()
    )

    results = {"processed": 0, "errors": []}

    for conf in pending:
        try:
            confirmed_date = conf.confirmed_at
            year = str(confirmed_date.year)
            month = f"{confirmed_date.month:02d}"

            paths = download_receipt(
                thread_id=conf.gmail_thread_id,
                ad_number=conf.ad.ad_number if conf.ad else str(conf.ad_id),
                year=year,
                month=month,
            )

            if paths:
                conf.receipt_file_path = paths[0]           # Primary receipt
                conf.receipt_url = ",".join(paths)          # All if multiple
                results["processed"] += 1

        except Exception as e:
            results["errors"].append(f"Confirmation {conf.id}: {str(e)}")

    db_session.commit()
    return results
