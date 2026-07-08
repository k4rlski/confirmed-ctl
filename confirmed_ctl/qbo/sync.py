"""confirmed_ctl/qbo/sync.py

Pulls Purchase transactions from QBO and upserts into bank_transactions table.
Designed to run as a cron job or triggered via CLI / Flask sync button.
"""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..db.models import BankTransaction, SyncLog
from .client import qbo_get


def sync_recent_transactions(
    db: Session,
    lookback_days: int = 2,
    use_cdc: bool = True,
) -> dict:
    """
    Main sync entry point. Called by daemon, cron, or Flask sync button.
    Returns summary dict for logging and UI display.
    """
    started_at = datetime.now(timezone.utc)
    summary = {"fetched": 0, "new": 0, "updated": 0, "errors": []}

    try:
        if use_cdc:
            raw_txns = _fetch_via_cdc(db, lookback_days)
        else:
            raw_txns = _fetch_via_date_query(lookback_days)

        summary["fetched"] = len(raw_txns)

        for raw in raw_txns:
            try:
                result = _upsert_transaction(db, raw)
                if result == "new":
                    summary["new"] += 1
                elif result == "updated":
                    summary["updated"] += 1
            except Exception as e:
                summary["errors"].append(f"QBO ID {raw.get('Id')}: {str(e)}")

        db.commit()

    except Exception as e:
        db.rollback()
        summary["errors"].append(f"Sync failed: {str(e)}")

    # Write sync log
    duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
    log = SyncLog(
        lookback_days=lookback_days,
        txns_fetched=summary["fetched"],
        txns_new=summary["new"],
        txns_updated=summary["updated"],
        errors="\n".join(summary["errors"]) if summary["errors"] else None,
        duration_ms=duration_ms,
    )
    db.add(log)
    db.commit()

    return summary


def _fetch_via_date_query(lookback_days: int) -> list[dict]:
    since_date = (date.today() - timedelta(days=lookback_days)).isoformat()
    query = (
        f"SELECT * FROM Purchase "
        f"WHERE TxnDate >= '{since_date}' "
        f"ORDERBY TxnDate DESC MAXRESULTS 200"
    )
    result = qbo_get("query", {"query": query})
    return result.get("QueryResponse", {}).get("Purchase", [])


def _fetch_via_cdc(db: Session, fallback_days: int) -> list[dict]:
    """
    Use CDC for incremental sync. Falls back to date query on first run.
    Reads last successful sync time from sync log table.
    """
    last_sync = (
        db.query(SyncLog)
        .filter(SyncLog.errors.is_(None))
        .order_by(SyncLog.synced_at.desc())
        .first()
    )

    if last_sync and last_sync.synced_at:
        since = last_sync.synced_at.isoformat()
    else:
        since = (datetime.now(timezone.utc) - timedelta(days=fallback_days)).isoformat()

    result = qbo_get("cdc", {
        "entities": "Purchase,BillPayment",
        "changedSince": since,
    })
    cdc = result.get("CDCResponse", [{}])[0].get("QueryResponse", [])
    purchases = []
    for group in cdc:
        for entity_type in ("Purchase", "BillPayment"):
            if entity_type in group:
                purchases.extend(group[entity_type])
    return purchases


def _upsert_transaction(db: Session, raw: dict) -> str:
    """Insert or update a single transaction. Returns 'new' or 'updated'."""
    qbo_id = raw.get("Id")
    lines = raw.get("Line", [])
    line_descs = [ln.get("Description", "") for ln in lines if ln.get("Description")]

    existing = db.query(BankTransaction).filter_by(qbo_id=qbo_id).first()

    data = dict(
        qbo_id          = qbo_id,
        sync_token      = raw.get("SyncToken"),
        txn_date        = raw.get("TxnDate"),
        created_time    = raw.get("MetaData", {}).get("CreateTime"),
        updated_time    = raw.get("MetaData", {}).get("LastUpdatedTime"),
        total_amount    = float(raw.get("TotalAmt", 0)),
        payment_type    = raw.get("PaymentType"),
        payment_ref_num = raw.get("PaymentRefNum"),
        private_note    = raw.get("PrivateNote"),
        doc_number      = raw.get("DocNumber"),
        vendor_id       = raw.get("EntityRef", {}).get("value"),
        vendor_name     = raw.get("EntityRef", {}).get("name"),
        account_id      = raw.get("AccountRef", {}).get("value"),
        account_name    = raw.get("AccountRef", {}).get("name"),
        line_descriptions = line_descs,
        raw_json        = raw,
    )

    if existing:
        for k, v in data.items():
            setattr(existing, k, v)
        return "updated"
    else:
        db.add(BankTransaction(**data))
        return "new"
