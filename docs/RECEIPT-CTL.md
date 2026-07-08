# receipt-ctl — Standalone Tool Specification
## Repo: `receipt-ctl` | Host: `prx.auto-ops.net` | Suite: auto-ctl.io
**Prepared:** June 18, 2026 | Handoff target: Cursor

---

## 1. Purpose & Scope

`receipt-ctl` is a single-purpose daemon and CLI tool that:

1. Reads `ad_confirmations` rows where a `gmail_thread_id` has been stored
   by `confirmed-ctl` but no receipt has yet been retrieved
2. Opens those Gmail threads, downloads all receipt attachments
3. Moves files to a structured directory tree on local or cloud storage
4. Writes the storage path/URL and file metadata back to the database
5. Logs every run and every file operation to its own log table
6. Exposes a Flask-triggerable report page where any receipt can be
   downloaded on demand

**What it does not do:**
- It does not search Gmail (that is `confirmed-ctl`'s job)
- It does not match transactions to ads (that is `confirmed-ctl`'s job)
- It does not categorize transactions in QuickBooks (that is a future
  tool — working name `qbo-ctl` or `ledger-ctl` — scoped separately)
- It does not know or care how `ad_confirmations` was populated

**Interface with other tools:**
- Input: reads `ad_confirmations.gmail_thread_id` (written by `confirmed-ctl`)
- Output: writes `receipt_files` rows + updates `ad_confirmations.receipt_status`
- Both tools speak only through the shared Postgres database. No imports,
  no submodules, no RPC. This is the auto-ctl.io standard.

---

## 2. Repo Structure

```
/opt/receipt-ctl/
├── receipt_ctl/
│   ├── __init__.py
│   ├── cli.py                  # Click CLI entry point
│   ├── daemon.py               # Daemon loop (runs on schedule)
│   ├── processor.py            # Core: fetch threads → download → store
│   ├── gmail/
│   │   ├── client.py           # Gmail OAuth + attachment fetch
│   │   └── parser.py           # MIME parsing, file type detection
│   ├── storage/
│   │   ├── local.py            # Local filesystem handler
│   │   └── cloud.py            # Cloud storage (S3-compatible or GCS)
│   ├── db/
│   │   ├── models.py           # SQLAlchemy models for receipt-ctl tables
│   │   ├── session.py          # DB connection via DATABASE_URL
│   │   └── migrations/         # Alembic migrations
│   └── report/
│       └── routes.py           # Flask blueprint: receipt report + download
├── .env
├── .env.example
├── requirements.txt
├── setup.py                    # Installs `receipt-ctl` CLI command
├── receipt-ctl.service         # systemd unit
└── README.md
```

---

## 3. Database Schema

These tables live in the same Postgres instance as `confirmed-ctl` and
your main CRM. `receipt-ctl` owns `receipt_files` and `receipt_ctl_log`.
It reads from `ad_confirmations` (owned by `confirmed-ctl`) but only
writes to columns explicitly reserved for it.

### 3.1 `receipt_files` — One row per downloaded file

```sql
CREATE TABLE IF NOT EXISTS receipt_files (
    id                  SERIAL PRIMARY KEY,

    -- Relationship to confirmed-ctl
    ad_confirmation_id  INTEGER         NOT NULL
                            REFERENCES ad_confirmations(id) ON DELETE CASCADE,
    ad_id               INTEGER         NOT NULL,   -- denormalized for query convenience
    ad_number           VARCHAR(100),

    -- Gmail source
    gmail_thread_id     VARCHAR(255)    NOT NULL,
    gmail_message_id    VARCHAR(255),               -- specific message attachment came from
    gmail_subject       TEXT,

    -- File identity
    original_filename   VARCHAR(500)    NOT NULL,
    mime_type           VARCHAR(100),               -- application/pdf, image/jpeg, etc.
    file_size_bytes     BIGINT,
    file_hash           VARCHAR(64),                -- SHA-256 of file contents (dedup)

    -- Storage
    storage_backend     VARCHAR(20)     NOT NULL DEFAULT 'local',  -- 'local' or 's3' or 'gcs'
    local_path          TEXT,           -- /mnt/receipts/2026/06/AD-1234/filename.pdf
    cloud_bucket        VARCHAR(255),
    cloud_key           TEXT,           -- path within bucket
    cloud_url           TEXT,           -- public or presigned URL
    storage_confirmed   BOOLEAN         DEFAULT FALSE,  -- set TRUE after write verified

    -- Status
    download_status     VARCHAR(20)     DEFAULT 'pending',
                                        -- pending, downloaded, failed, skipped
    error_message       TEXT,

    -- Timestamps
    downloaded_at       TIMESTAMPTZ,
    created_in_db       TIMESTAMPTZ     DEFAULT NOW()
);

CREATE INDEX idx_receipt_files_ad          ON receipt_files(ad_id);
CREATE INDEX idx_receipt_files_thread      ON receipt_files(gmail_thread_id);
CREATE INDEX idx_receipt_files_status      ON receipt_files(download_status);
CREATE INDEX idx_receipt_files_hash        ON receipt_files(file_hash);  -- dedup queries

-- Prevent re-downloading the exact same attachment
CREATE UNIQUE INDEX uidx_receipt_file_dedup
    ON receipt_files(gmail_message_id, original_filename)
    WHERE gmail_message_id IS NOT NULL;
```

### 3.2 `receipt_ctl_log` — Audit trail for every run

```sql
CREATE TABLE IF NOT EXISTS receipt_ctl_log (
    id              SERIAL PRIMARY KEY,
    run_at          TIMESTAMPTZ     DEFAULT NOW(),
    trigger         VARCHAR(30),    -- 'daemon', 'cron', 'cli', 'flask_button'
    ads_scanned     INTEGER         DEFAULT 0,
    receipts_found  INTEGER         DEFAULT 0,
    receipts_saved  INTEGER         DEFAULT 0,
    receipts_failed INTEGER         DEFAULT 0,
    receipts_skipped INTEGER        DEFAULT 0,  -- already downloaded (dedup)
    storage_bytes   BIGINT          DEFAULT 0,
    errors          TEXT,
    duration_ms     INTEGER
);
```

### 3.3 Column reserved on `ad_confirmations` for receipt-ctl

```sql
-- Add these columns to confirmed-ctl's ad_confirmations table via migration.
-- receipt-ctl owns these columns; confirmed-ctl leaves them NULL.
ALTER TABLE ad_confirmations
    ADD COLUMN IF NOT EXISTS receipt_status     VARCHAR(20) DEFAULT 'pending',
    -- pending | downloaded | no_attachment | failed
    ADD COLUMN IF NOT EXISTS receipt_checked_at TIMESTAMPTZ;
```

---

## 4. Core Processor — `receipt_ctl/processor.py`

```python
"""
receipt_ctl/processor.py

Main processing loop. Called by daemon, cron, or CLI.

Flow:
  1. Query ad_confirmations for rows with gmail_thread_id and receipt_status = 'pending'
  2. For each: open Gmail thread, find attachments, download, store, update DB
  3. Log everything to receipt_ctl_log
"""
import hashlib
import time
import logging
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from .gmail.client import get_gmail_service
from .gmail.parser import extract_attachments
from .storage.local import save_to_local
from .storage.cloud import save_to_cloud
from .db.models import AdConfirmation, ReceiptFile, ReceiptCtlLog

log = logging.getLogger("receipt-ctl.processor")


def run_receipt_pass(
    db: Session,
    storage_backend: str = "local",   # 'local' or 's3' or 'gcs'
    trigger: str = "daemon",
    limit: int = 50,
) -> dict:
    """
    Main entry point. Process up to `limit` pending confirmations per run.
    Returns summary dict written to receipt_ctl_log.
    """
    started_at = datetime.now(timezone.utc)
    summary = {
        "ads_scanned":     0,
        "receipts_found":  0,
        "receipts_saved":  0,
        "receipts_failed": 0,
        "receipts_skipped": 0,
        "storage_bytes":   0,
        "errors":          [],
    }

    # Fetch pending confirmations
    pending = db.query(AdConfirmation).filter(
        AdConfirmation.gmail_thread_id != None,
        AdConfirmation.receipt_status == "pending",
    ).limit(limit).all()

    summary["ads_scanned"] = len(pending)

    if not pending:
        log.info("No pending receipts to process.")
        _write_log(db, trigger, summary, started_at)
        return summary

    service = get_gmail_service()

    for conf in pending:
        try:
            _process_confirmation(db, conf, service, storage_backend, summary)
        except Exception as e:
            log.error(f"Failed processing confirmation {conf.id}: {e}")
            summary["errors"].append(f"confirmation_id={conf.id}: {str(e)}")
            conf.receipt_status = "failed"

    db.commit()
    _write_log(db, trigger, summary, started_at)
    return summary


def _process_confirmation(
    db: Session,
    conf: AdConfirmation,
    service,
    storage_backend: str,
    summary: dict,
):
    log.info(f"Processing confirmation {conf.id}, thread {conf.gmail_thread_id}")

    # Fetch full thread
    thread = service.users().threads().get(
        userId="me",
        id=conf.gmail_thread_id,
        format="full",
    ).execute()

    attachments = extract_attachments(service, thread)
    summary["receipts_found"] += len(attachments)

    if not attachments:
        log.info(f"No attachments found in thread {conf.gmail_thread_id}")
        conf.receipt_status    = "no_attachment"
        conf.receipt_checked_at = datetime.now(timezone.utc)
        return

    for att in attachments:
        file_hash = hashlib.sha256(att["data"]).hexdigest()

        # Dedup check — don't re-download the same file
        existing = db.query(ReceiptFile).filter_by(
            gmail_message_id  = att["message_id"],
            original_filename = att["filename"],
        ).first()
        if existing:
            log.info(f"Skipping duplicate: {att['filename']}")
            summary["receipts_skipped"] += 1
            continue

        # Determine storage path
        year  = conf.confirmed_at.strftime("%Y") if conf.confirmed_at else "unknown"
        month = conf.confirmed_at.strftime("%m") if conf.confirmed_at else "unknown"
        ad_number = conf.ad.ad_number if conf.ad else str(conf.ad_id)
        rel_path = f"{year}/{month}/{ad_number}/{att['filename']}"

        receipt = ReceiptFile(
            ad_confirmation_id = conf.id,
            ad_id              = conf.ad_id,
            ad_number          = ad_number,
            gmail_thread_id    = conf.gmail_thread_id,
            gmail_message_id   = att["message_id"],
            gmail_subject      = conf.gmail_subject,
            original_filename  = att["filename"],
            mime_type          = att["mime_type"],
            file_size_bytes    = len(att["data"]),
            file_hash          = file_hash,
            storage_backend    = storage_backend,
            download_status    = "pending",
        )

        try:
            if storage_backend == "local":
                path = save_to_local(att["data"], rel_path)
                receipt.local_path         = path
                receipt.storage_confirmed  = True
            else:
                url, bucket, key = save_to_cloud(att["data"], rel_path, storage_backend)
                receipt.cloud_url          = url
                receipt.cloud_bucket       = bucket
                receipt.cloud_key          = key
                receipt.storage_confirmed  = True

            receipt.download_status = "downloaded"
            receipt.downloaded_at   = datetime.now(timezone.utc)
            summary["receipts_saved"]   += 1
            summary["storage_bytes"]    += len(att["data"])

        except Exception as e:
            receipt.download_status = "failed"
            receipt.error_message   = str(e)
            summary["receipts_failed"] += 1
            log.error(f"Storage failed for {att['filename']}: {e}")

        db.add(receipt)

    # Mark confirmation as processed regardless of individual file results
    conf.receipt_status     = "downloaded"
    conf.receipt_checked_at = datetime.now(timezone.utc)


def _write_log(db: Session, trigger: str, summary: dict, started_at: datetime):
    duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
    entry = ReceiptCtlLog(
        trigger           = trigger,
        ads_scanned       = summary["ads_scanned"],
        receipts_found    = summary["receipts_found"],
        receipts_saved    = summary["receipts_saved"],
        receipts_failed   = summary["receipts_failed"],
        receipts_skipped  = summary["receipts_skipped"],
        storage_bytes     = summary["storage_bytes"],
        errors            = "\n".join(summary["errors"]) if summary["errors"] else None,
        duration_ms       = duration_ms,
    )
    db.add(entry)
    db.commit()
```

---

## 5. Gmail Attachment Parser — `receipt_ctl/gmail/parser.py`

```python
"""
receipt_ctl/gmail/parser.py

Extract all downloadable attachments from a Gmail thread dict.
Handles both inline attachments and multipart MIME structures.
"""
import base64
import logging

log = logging.getLogger("receipt-ctl.parser")

# File types we want to save as receipts
RECEIPT_MIME_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/tiff",
    "application/octet-stream",   # Sometimes PDFs arrive as this
}


def extract_attachments(service, thread: dict) -> list[dict]:
    """
    Walk every message in the thread.
    Return list of dicts: {data, filename, mime_type, message_id}
    """
    results = []
    for message in thread.get("messages", []):
        message_id = message["id"]
        _walk_parts(service, message_id, message.get("payload", {}), results)
    return results


def _walk_parts(service, message_id: str, payload: dict, results: list):
    """Recursively walk MIME parts."""
    mime_type = payload.get("mimeType", "")
    filename  = payload.get("filename", "")
    body      = payload.get("body", {})

    # Inline attachment with data
    if filename and body.get("data"):
        if mime_type in RECEIPT_MIME_TYPES or _is_receipt_filename(filename):
            raw = base64.urlsafe_b64decode(body["data"])
            results.append({
                "data":       raw,
                "filename":   _sanitize_filename(filename),
                "mime_type":  mime_type,
                "message_id": message_id,
            })

    # Attachment stored separately (requires extra API call)
    elif filename and body.get("attachmentId"):
        if mime_type in RECEIPT_MIME_TYPES or _is_receipt_filename(filename):
            try:
                att = service.users().messages().attachments().get(
                    userId="me",
                    messageId=message_id,
                    id=body["attachmentId"],
                ).execute()
                raw = base64.urlsafe_b64decode(att["data"])
                results.append({
                    "data":       raw,
                    "filename":   _sanitize_filename(filename),
                    "mime_type":  mime_type,
                    "message_id": message_id,
                })
            except Exception as e:
                log.warning(f"Could not fetch attachment {filename}: {e}")

    # Recurse into nested parts
    for part in payload.get("parts", []):
        _walk_parts(service, message_id, part, results)


def _is_receipt_filename(filename: str) -> bool:
    """Accept files that look like receipts even if MIME type is generic."""
    lower = filename.lower()
    return any(lower.endswith(ext) for ext in [".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff"])


def _sanitize_filename(filename: str) -> str:
    """Remove characters unsafe for filesystem paths."""
    import re
    safe = re.sub(r'[^\w\-_. ]', '_', filename)
    return safe.strip()
```

---

## 6. Storage Handlers

### `receipt_ctl/storage/local.py`

```python
"""
receipt_ctl/storage/local.py

Save receipt file to local filesystem.
Base path configured via RECEIPTS_BASE_PATH env var.
"""
import os
from pathlib import Path

RECEIPTS_BASE = os.environ.get("RECEIPTS_BASE_PATH", "/mnt/receipts")


def save_to_local(data: bytes, relative_path: str) -> str:
    """
    Save bytes to RECEIPTS_BASE/relative_path.
    Creates parent directories as needed.
    Returns the absolute path.
    """
    full_path = Path(RECEIPTS_BASE) / relative_path
    full_path.parent.mkdir(parents=True, exist_ok=True)

    # Atomic write: write to .tmp then rename
    tmp_path = full_path.with_suffix(full_path.suffix + ".tmp")
    with open(tmp_path, "wb") as f:
        f.write(data)
    tmp_path.rename(full_path)

    return str(full_path)
```

### `receipt_ctl/storage/cloud.py`

```python
"""
receipt_ctl/storage/cloud.py

S3-compatible and GCS upload handler.
Supports any S3-compatible backend (AWS S3, Backblaze B2, Cloudflare R2, MinIO).
Backend selected by STORAGE_BACKEND env var: 's3', 'gcs', 'r2', 'b2'
"""
import os
import boto3
from botocore.config import Config


def save_to_cloud(
    data: bytes,
    relative_path: str,
    backend: str = "s3",
) -> tuple[str, str, str]:
    """
    Upload bytes to cloud storage.
    Returns (public_or_presigned_url, bucket, key).
    """
    bucket = os.environ["STORAGE_BUCKET"]
    prefix = os.environ.get("STORAGE_KEY_PREFIX", "receipts")
    key    = f"{prefix}/{relative_path}"

    if backend in ("s3", "r2", "b2"):
        client = _get_s3_client(backend)
        client.put_object(
            Bucket      = bucket,
            Key         = key,
            Body        = data,
            ContentType = _guess_content_type(relative_path),
        )
        # Generate presigned URL valid for 7 days (adjust as needed)
        url = client.generate_presigned_url(
            "get_object",
            Params     = {"Bucket": bucket, "Key": key},
            ExpiresIn  = 604800,
        )
    elif backend == "gcs":
        url, bucket, key = _save_to_gcs(data, bucket, key, relative_path)
    else:
        raise ValueError(f"Unknown storage backend: {backend}")

    return url, bucket, key


def _get_s3_client(backend: str):
    kwargs = dict(
        aws_access_key_id     = os.environ["STORAGE_ACCESS_KEY"],
        aws_secret_access_key = os.environ["STORAGE_SECRET_KEY"],
        region_name           = os.environ.get("STORAGE_REGION", "us-east-1"),
    )
    endpoint = os.environ.get("STORAGE_ENDPOINT_URL")
    if endpoint:
        kwargs["endpoint_url"] = endpoint   # For R2, B2, MinIO
    return boto3.client("s3", **kwargs)


def _save_to_gcs(data: bytes, bucket: str, key: str, rel_path: str):
    from google.cloud import storage as gcs
    client = gcs.Client()
    blob   = client.bucket(bucket).blob(key)
    blob.upload_from_string(data, content_type=_guess_content_type(rel_path))
    url    = blob.generate_signed_url(expiration=604800)
    return url, bucket, key


def _guess_content_type(path: str) -> str:
    lower = path.lower()
    if lower.endswith(".pdf"):  return "application/pdf"
    if lower.endswith(".png"):  return "image/png"
    if lower.endswith(".jpg") or lower.endswith(".jpeg"): return "image/jpeg"
    return "application/octet-stream"
```

---

## 7. CLI Entry Point — `receipt_ctl/cli.py`

```python
"""
receipt_ctl/cli.py

CLI for receipt-ctl.

Usage:
  receipt-ctl run [--limit 50] [--storage local|s3|gcs|r2]
  receipt-ctl status
  receipt-ctl retry --ad-id 1234
  receipt-ctl show --ad-id 1234
"""
import click
from .db.session import get_db
from .processor import run_receipt_pass


@click.group()
def cli():
    """receipt-ctl — Gmail receipt downloader and storage tool. auto-ctl.io suite."""
    pass


@cli.command()
@click.option("--limit",   default=50,      help="Max confirmations to process per run")
@click.option("--storage", default="local", help="Storage backend: local, s3, gcs, r2, b2")
@click.option("--trigger", default="cli",   help="Trigger label for log: cli, cron, flask")
def run(limit, storage, trigger):
    """Process pending receipt downloads."""
    click.echo(f"receipt-ctl run | storage={storage} limit={limit}")
    with get_db() as db:
        summary = run_receipt_pass(db, storage_backend=storage, trigger=trigger, limit=limit)
    click.echo(
        f"Done: {summary['receipts_saved']} saved, "
        f"{summary['receipts_failed']} failed, "
        f"{summary['receipts_skipped']} skipped, "
        f"{summary['storage_bytes']:,} bytes."
    )
    if summary["errors"]:
        click.echo("Errors:")
        for e in summary["errors"]:
            click.echo(f"  {e}")


@cli.command()
def status():
    """Show last run stats from receipt_ctl_log."""
    with get_db() as db:
        from .db.models import ReceiptCtlLog
        last = db.query(ReceiptCtlLog).order_by(ReceiptCtlLog.run_at.desc()).first()
        if not last:
            click.echo("No runs recorded yet.")
            return
        click.echo(f"Last run:  {last.run_at}")
        click.echo(f"Trigger:   {last.trigger}")
        click.echo(f"Scanned:   {last.ads_scanned}")
        click.echo(f"Saved:     {last.receipts_saved}")
        click.echo(f"Failed:    {last.receipts_failed}")
        click.echo(f"Skipped:   {last.receipts_skipped}")
        click.echo(f"Bytes:     {last.storage_bytes:,}")
        if last.errors:
            click.echo(f"Errors:\n{last.errors}")


@cli.command()
@click.option("--ad-id", required=True, type=int, help="Reset receipt_status to pending for this ad")
def retry(ad_id):
    """Force re-processing of receipts for a specific ad confirmation."""
    with get_db() as db:
        from .db.models import AdConfirmation
        conf = db.query(AdConfirmation).filter_by(ad_id=ad_id).first()
        if not conf:
            click.echo(f"No confirmation found for ad_id={ad_id}")
            return
        conf.receipt_status = "pending"
        db.commit()
        click.echo(f"Reset receipt_status to 'pending' for ad_id={ad_id}. Run `receipt-ctl run` to process.")


@cli.command()
@click.option("--ad-id", required=True, type=int)
def show(ad_id):
    """Show all downloaded receipt files for an ad."""
    with get_db() as db:
        from .db.models import ReceiptFile
        files = db.query(ReceiptFile).filter_by(ad_id=ad_id).all()
        if not files:
            click.echo(f"No receipt files found for ad_id={ad_id}")
            return
        for f in files:
            click.echo(
                f"  [{f.download_status}] {f.original_filename} "
                f"({f.file_size_bytes:,} bytes) "
                f"{'local: ' + f.local_path if f.local_path else 'cloud: ' + (f.cloud_url or 'none')}"
            )


if __name__ == "__main__":
    cli()
```

---

## 8. Flask Report Blueprint — `receipt_ctl/report/routes.py`

```python
"""
receipt_ctl/report/routes.py

Flask blueprint for the receipt-ctl report page.
Mount in main app:
    from receipt_ctl.report.routes import receipt_ctl_bp
    app.register_blueprint(receipt_ctl_bp)

Routes:
  GET  /receipts/                  — report: all downloaded receipts, filterable
  GET  /receipts/ad/<ad_id>        — receipts for one ad
  GET  /receipts/download/<file_id> — serve file (local) or redirect (cloud)
  POST /receipts/sync              — trigger receipt-ctl run (Sync button)
  GET  /receipts/log               — last N log entries for status display
"""
import os
from flask import Blueprint, jsonify, request, send_file, redirect, abort
from ..db.session import get_db
from ..db.models import ReceiptFile, ReceiptCtlLog, AdConfirmation
from ..processor import run_receipt_pass

receipt_ctl_bp = Blueprint("receipt_ctl", __name__, url_prefix="/receipts")


@receipt_ctl_bp.route("/", methods=["GET"])
def report():
    """
    Return paginated list of receipt files.
    Query params: page, per_page, status, ad_number
    """
    page     = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 50))
    status   = request.args.get("status")         # downloaded, failed, pending
    ad_num   = request.args.get("ad_number")

    with get_db() as db:
        q = db.query(ReceiptFile)
        if status:
            q = q.filter(ReceiptFile.download_status == status)
        if ad_num:
            q = q.filter(ReceiptFile.ad_number.ilike(f"%{ad_num}%"))
        total = q.count()
        files = q.order_by(ReceiptFile.downloaded_at.desc())\
                 .offset((page - 1) * per_page)\
                 .limit(per_page)\
                 .all()

    return jsonify({
        "total":   total,
        "page":    page,
        "results": [_serialize_file(f) for f in files],
    })


@receipt_ctl_bp.route("/ad/<int:ad_id>", methods=["GET"])
def receipts_for_ad(ad_id):
    """All receipt files for a single ad."""
    with get_db() as db:
        files = db.query(ReceiptFile).filter_by(ad_id=ad_id).all()
    return jsonify([_serialize_file(f) for f in files])


@receipt_ctl_bp.route("/download/<int:file_id>", methods=["GET"])
def download_receipt(file_id):
    """
    Serve a receipt file.
    - Local storage: streams file directly via send_file
    - Cloud storage: redirects to presigned URL
    """
    with get_db() as db:
        f = db.query(ReceiptFile).get(file_id)
        if not f:
            abort(404)

        if f.storage_backend == "local" and f.local_path:
            if not os.path.exists(f.local_path):
                abort(404, description="File not found on disk")
            return send_file(
                f.local_path,
                download_name = f.original_filename,
                as_attachment = True,
                mimetype      = f.mime_type or "application/octet-stream",
            )
        elif f.cloud_url:
            return redirect(f.cloud_url)
        else:
            abort(404, description="No storage path available")


@receipt_ctl_bp.route("/sync", methods=["POST"])
def trigger_sync():
    """
    Sync button handler — runs receipt-ctl pass immediately.
    Mirrors the confirmed-ctl sync button pattern.
    """
    body    = request.get_json(silent=True) or {}
    storage = body.get("storage", os.environ.get("STORAGE_BACKEND", "local"))
    limit   = int(body.get("limit", 50))

    with get_db() as db:
        summary = run_receipt_pass(db, storage_backend=storage, trigger="flask_button", limit=limit)

    return jsonify({"status": "ok", "summary": summary})


@receipt_ctl_bp.route("/log", methods=["GET"])
def get_log():
    """Last N run log entries for status display in UI."""
    n = int(request.args.get("n", 10))
    with get_db() as db:
        entries = db.query(ReceiptCtlLog)\
                    .order_by(ReceiptCtlLog.run_at.desc())\
                    .limit(n)\
                    .all()
    return jsonify([{
        "run_at":           e.run_at.isoformat(),
        "trigger":          e.trigger,
        "ads_scanned":      e.ads_scanned,
        "receipts_saved":   e.receipts_saved,
        "receipts_failed":  e.receipts_failed,
        "receipts_skipped": e.receipts_skipped,
        "storage_bytes":    e.storage_bytes,
        "duration_ms":      e.duration_ms,
        "errors":           e.errors,
    } for e in entries])


def _serialize_file(f: ReceiptFile) -> dict:
    return {
        "id":               f.id,
        "ad_id":            f.ad_id,
        "ad_number":        f.ad_number,
        "filename":         f.original_filename,
        "mime_type":        f.mime_type,
        "size_bytes":       f.file_size_bytes,
        "status":           f.download_status,
        "storage_backend":  f.storage_backend,
        "local_path":       f.local_path,
        "cloud_url":        f.cloud_url,
        "gmail_subject":    f.gmail_subject,
        "downloaded_at":    f.downloaded_at.isoformat() if f.downloaded_at else None,
        "download_url":     f"/receipts/download/{f.id}",  # internal Flask route
    }
```

---

## 9. Daemon & systemd

### `receipt_ctl/daemon.py`

```python
"""
receipt_ctl/daemon.py

Daemon loop for receipt-ctl.
Wakes up at configured interval, processes pending receipts, sleeps.
Interval configured via RECEIPT_CTL_INTERVAL_SECONDS (default: 3600 = 1 hour).
"""
import os
import time
import logging
from .db.session import get_db
from .processor import run_receipt_pass

INTERVAL = int(os.environ.get("RECEIPT_CTL_INTERVAL_SECONDS", 3600))
STORAGE  = os.environ.get("STORAGE_BACKEND", "local")
log      = logging.getLogger("receipt-ctl.daemon")


def run():
    log.info(f"receipt-ctl daemon starting. Interval: {INTERVAL}s. Storage: {STORAGE}.")
    while True:
        try:
            with get_db() as db:
                summary = run_receipt_pass(db, storage_backend=STORAGE, trigger="daemon")
            log.info(
                f"Pass complete: {summary['receipts_saved']} saved, "
                f"{summary['receipts_failed']} failed, "
                f"{summary['storage_bytes']:,} bytes."
            )
        except Exception as e:
            log.error(f"Daemon pass error: {e}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
```

### `receipt-ctl.service`

```ini
[Unit]
Description=receipt-ctl Gmail receipt downloader daemon
After=network.target postgresql.service confirmed-ctl.service

[Service]
Type=simple
User=auto-ops
WorkingDirectory=/opt/receipt-ctl
EnvironmentFile=/opt/receipt-ctl/.env
ExecStart=/opt/receipt-ctl/venv/bin/python -m receipt_ctl.daemon
Restart=on-failure
RestartSec=60
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### Cron fallback (if not using daemon)

```bash
# /etc/cron.d/receipt_ctl
# Run at 8:15 AM daily — after confirmed-ctl sync at 7:30 AM
15 8 * * * auto-ops /opt/receipt-ctl/venv/bin/receipt-ctl run \
  --storage local --trigger cron >> /var/log/auto-ops/receipt-ctl.log 2>&1
```

---

## 10. `.env.example`

```bash
# Database (shared with confirmed-ctl and main CRM)
DATABASE_URL=postgresql://user:password@localhost:5432/your_db

# Gmail (can share token with confirmed-ctl if same Gmail account)
GMAIL_TOKEN_PATH=/opt/receipt-ctl/gmail_token.json

# Storage backend: local, s3, gcs, r2, b2
STORAGE_BACKEND=local
RECEIPTS_BASE_PATH=/mnt/receipts

# Cloud storage (only needed if STORAGE_BACKEND != local)
STORAGE_BUCKET=your-receipts-bucket
STORAGE_ACCESS_KEY=
STORAGE_SECRET_KEY=
STORAGE_REGION=us-east-1
STORAGE_ENDPOINT_URL=        # For R2: https://ACCOUNT.r2.cloudflarestorage.com
                             # For B2: https://s3.us-west-004.backblazeb2.com
STORAGE_KEY_PREFIX=receipts

# Daemon interval
RECEIPT_CTL_INTERVAL_SECONDS=3600
```

---

## 11. `requirements.txt`

```
# Database
sqlalchemy>=2.0
alembic>=1.13
psycopg2-binary>=2.9

# Gmail
google-auth>=2.28
google-auth-oauthlib>=1.2
google-api-python-client>=2.120

# Cloud storage
boto3>=1.34              # S3 / R2 / B2 (all S3-compatible)
google-cloud-storage>=2.16   # GCS only — omit if not using GCS

# Flask
flask>=3.0

# CLI
click>=8.1

# Utils
python-dotenv>=1.0
```

---

## 12. Future Handoff Point to `qbo-ctl` (or `ledger-ctl`)

Once `receipt-ctl` has downloaded and stored a receipt, the next logical
operation is to write the correct accounting classification back to QuickBooks:
marking the transaction as Cost of Goods Sold, Cost of Services, or the
appropriate expense account for the ad type.

`receipt-ctl` deliberately stops at storage. It will write a flag column
to signal readiness:

```sql
ALTER TABLE ad_confirmations
    ADD COLUMN IF NOT EXISTS qbo_categorized       BOOLEAN     DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS qbo_categorized_at    TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS qbo_category_account  VARCHAR(100);
```

The future `qbo-ctl` (or `ledger-ctl`) will:
- Read `ad_confirmations` where `receipt_status = 'downloaded'`
  and `qbo_categorized = FALSE`
- Look up the correct expense account from a config or rules table
  (COGS for newspaper ads, etc.)
- POST a sparse update to QBO `Purchase` entity with the correct
  `AccountRef` on the line items
- Write `qbo_categorized = TRUE` and `qbo_category_account` back
- Log to its own `qbo_ctl_log` table

That tool will be scoped separately. `receipt-ctl` leaves the door open
and nothing more.

---

## 13. Cursor Implementation Checklist

- [ ] Initialize repo `/opt/receipt-ctl/` with structure from Section 2
- [ ] `db/models.py` — `ReceiptFile`, `ReceiptCtlLog`; reference `AdConfirmation`
      from `confirmed-ctl` schema (same DB, no import)
- [ ] `db/session.py` — connect via `DATABASE_URL` env var
- [ ] Alembic migration: create `receipt_files`, `receipt_ctl_log`;
      alter `ad_confirmations` to add `receipt_status`, `receipt_checked_at`,
      `qbo_categorized`, `qbo_categorized_at`, `qbo_category_account`
- [ ] `gmail/client.py` — reuse or copy from `confirmed-ctl`; point at same
      `GMAIL_TOKEN_PATH`
- [ ] `gmail/parser.py` — MIME walker; test against a known newspaper email thread
- [ ] `storage/local.py` — atomic write with `.tmp` rename
- [ ] `storage/cloud.py` — S3-compatible first; GCS optional behind env flag
- [ ] `processor.py` — main loop with dedup check on `(gmail_message_id, filename)`
- [ ] `cli.py` — `run`, `status`, `retry`, `show` commands
- [ ] Install CLI: `pip install -e .` so `receipt-ctl` is on PATH in venv
- [ ] `report/routes.py` — Flask blueprint; register in main app
- [ ] Test `receipt-ctl run --storage local` against one real confirmed ad
      that has a `gmail_thread_id` in `ad_confirmations`
- [ ] Verify atomic write: check `/mnt/receipts/{year}/{month}/{ad_number}/`
- [ ] Verify DB row in `receipt_files` with correct path and hash
- [ ] Verify `ad_confirmations.receipt_status` updated to `downloaded`
- [ ] Verify log entry in `receipt_ctl_log`
- [ ] Install and enable `receipt-ctl.service` via systemd
- [ ] Set cron fallback in `/etc/cron.d/receipt_ctl` (belt and suspenders)
- [ ] Wire [Sync] button in Flask receipt report page → `POST /receipts/sync`
- [ ] Test download via `GET /receipts/download/<file_id>` for local file
- [ ] When cloud storage is ready: switch `STORAGE_BACKEND` in `.env` and
      re-run; verify presigned URL returned and redirect works
- [ ] Add `receipt_status` column filter to `confirmed-ctl` Confirmed-CTL
      report so undownloaded receipts are visually flagged

---

## 14. Relationship Map — auto-ctl.io Suite (current)

```
confirmed-ctl          receipt-ctl           [future: qbo-ctl / ledger-ctl]
─────────────          ───────────           ──────────────────────────────
Syncs QBO → DB    →    Reads thread IDs  →   Reads receipt_status=downloaded
Scores candidates      Downloads files       POSTs category update to QBO
Human confirms         Stores to disk/cloud  Marks qbo_categorized=TRUE
Writes thread ID       Writes file path      Logs to qbo_ctl_log
confirmed-ctl sync     receipt-ctl run       qbo-ctl categorize
             ↘               ↘                        ↘
              ──────── shared Postgres DB ─────────────
                       ad_confirmations
                       bank_transactions
                       receipt_files
                       [qbo_ctl_log — future]
```

---

*Document version: 1.0 — receipt-ctl standalone specification*
*auto-ctl.io suite | Prepared for Cursor handoff | June 18, 2026*
*Host: `prx.auto-ops.net` | Repo: `/opt/receipt-ctl/`*
