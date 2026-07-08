"""confirmed_ctl/api/routes.py

Flask blueprint exposing endpoints for the Confirmed-CTL popup UI.
Mount in your main Flask app:
    from confirmed_ctl.api.routes import confirmed_ctl_bp
    app.register_blueprint(confirmed_ctl_bp)
"""
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from ..db.models import AdConfirmation, AdPurchase, BankTransaction, SyncLog
from ..db.session import get_db
from ..gmail.client import search_threads_by_ad_number
from ..matching.rag import store_confirmed_match
from ..matching.scorer import get_candidate_transactions
from ..qbo.sync import sync_recent_transactions

confirmed_ctl_bp = Blueprint("confirmed_ctl", __name__, url_prefix="/confirmed-ctl")


@confirmed_ctl_bp.route("/sync", methods=["POST"])
def trigger_sync():
    """
    Called by the [Sync Now] button in the Flask UI.
    Mirrors your Stripe sync button pattern.
    """
    body = request.get_json(silent=True) or {}
    lookback = int(body.get("lookback_days", 2))

    with get_db() as db:
        summary = sync_recent_transactions(db, lookback_days=lookback)

    return jsonify({"status": "ok", "summary": summary})


@confirmed_ctl_bp.route("/sync/status", methods=["GET"])
def sync_status():
    """Returns last sync run info for UI status display."""
    with get_db() as db:
        last = db.query(SyncLog).order_by(SyncLog.synced_at.desc()).first()
        if not last:
            return jsonify({"last_sync": None})
        return jsonify({
            "last_sync": last.synced_at.isoformat() if last.synced_at else None,
            "txns_fetched": last.txns_fetched,
            "txns_new": last.txns_new,
            "auto_matched": last.auto_matched,
            "errors": last.errors,
        })


@confirmed_ctl_bp.route("/candidates/<int:ad_id>", methods=["GET"])
def get_candidates(ad_id: int):
    """
    Called when user clicks an ad number in the UI.
    Returns ranked bank transaction candidates + Gmail thread results.
    """
    with get_db() as db:
        ad = db.get(AdPurchase, ad_id)
        if not ad:
            return jsonify({"error": "Ad not found"}), 404

        # Bank transaction candidates (ranked)
        candidates = get_candidate_transactions(db, ad)

        # Gmail threads
        gmail_threads = []
        if ad.ad_number:
            try:
                gmail_threads = search_threads_by_ad_number(ad.ad_number)
            except Exception:
                gmail_threads = []  # Don't break the popup if Gmail fails

        return jsonify({
            "ad": {
                "id": ad.id,
                "ad_number": ad.ad_number,
                "newspaper_name": ad.newspaper_name,
                "expected_amount": float(ad.expected_amount) if ad.expected_amount else None,
                "run_date": str(ad.run_date),
                "client_name": ad.client_name,
            },
            "bank_candidates": [
                {
                    "txn_id": c["transaction"].id,
                    "qbo_id": c["transaction"].qbo_id,
                    "txn_date": str(c["transaction"].txn_date),
                    "amount": float(c["transaction"].total_amount),
                    "vendor_name": c["transaction"].vendor_name,
                    "account_name": c["transaction"].account_name,
                    "payment_ref": c["transaction"].payment_ref_num,
                    "memo": c["transaction"].private_note,
                    "score": round(c["score"], 3),
                    "score_pct": int(c["score"] * 100),
                }
                for c in candidates
            ],
            "gmail_threads": gmail_threads,
        })


@confirmed_ctl_bp.route("/confirm", methods=["POST"])
def confirm_ad():
    """
    Called when user clicks [CONFIRM & CLOSE] in the popup.
    Saves the relationship: ad <-> bank transaction <-> Gmail thread.
    """
    body = request.get_json()
    ad_id = body.get("ad_id")
    txn_id = body.get("bank_txn_id")       # internal DB id
    thread_id = body.get("gmail_thread_id")
    thread_subj = body.get("gmail_subject", "")
    confirmed_by = body.get("confirmed_by", "user")

    with get_db() as db:
        ad = db.get(AdPurchase, ad_id)
        txn = db.get(BankTransaction, txn_id) if txn_id else None

        if not ad:
            return jsonify({"error": "Ad not found"}), 404

        # Check for existing confirmation (idempotency)
        existing = db.query(AdConfirmation).filter_by(ad_id=ad_id).first()
        if existing:
            return jsonify({"error": "Ad already confirmed", "confirmation_id": existing.id}), 409

        # Create confirmation record
        conf = AdConfirmation(
            ad_id=ad_id,
            bank_txn_id=txn.id if txn else None,
            gmail_thread_id=thread_id,
            gmail_subject=thread_subj,
            confirmed_by=confirmed_by,
            confirmed_at=datetime.now(timezone.utc),
            match_method="manual",
            match_confidence="manual",
        )
        db.add(conf)

        # Link transaction back to ad
        if txn:
            txn.confirmed_ad_id = ad_id
            txn.confirmed_at = conf.confirmed_at

        db.commit()

        # Store in RAG for future pattern learning (best-effort — never blocks confirm)
        if txn:
            try:
                store_confirmed_match(
                    ad_id=ad_id,
                    ad_number=ad.ad_number,
                    newspaper_name=ad.newspaper_name,
                    expected_amount=float(ad.expected_amount) if ad.expected_amount else 0.0,
                    txn_amount=float(txn.total_amount),
                    txn_date=str(txn.txn_date),
                    txn_vendor=txn.vendor_name or "",
                    match_method="manual",
                )
            except Exception:
                pass

        return jsonify({
            "status": "confirmed",
            "confirmation_id": conf.id,
            "ad_number": ad.ad_number,
            "txn_qbo_id": txn.qbo_id if txn else None,
            "gmail_thread_id": thread_id,
        })


@confirmed_ctl_bp.route("/unconfirmed", methods=["GET"])
def list_unconfirmed():
    """
    Returns unconfirmed ads for the report table.
    Replaces or supplements the existing Advise Cash Flow report.
    """
    with get_db() as db:
        # Ads not yet in ad_confirmations
        confirmed_ad_ids = db.query(AdConfirmation.ad_id).subquery()
        ads = (
            db.query(AdPurchase)
            .filter(AdPurchase.id.notin_(confirmed_ad_ids))
            .order_by(AdPurchase.run_date.desc())
            .limit(100)
            .all()
        )

        return jsonify([{
            "id": a.id,
            "ad_number": a.ad_number,
            "client_name": a.client_name,
            "newspaper_name": a.newspaper_name,
            "run_date": str(a.run_date),
            "expected_amount": float(a.expected_amount) if a.expected_amount else None,
            "status": "unconfirmed",
        } for a in ads])
