"""confirmed_ctl/api/routes.py

Flask blueprint exposing endpoints for the Confirmed-CTL popup UI.
Mount in your main Flask app:
    from confirmed_ctl.api.routes import confirmed_ctl_bp
    app.register_blueprint(confirmed_ctl_bp)

Cross-DB note: ad / case data lives ONLY in the MariaDB CRM
(``permtrak2_crm.t_e_s_t_p_e_r_m``, read-only) and is referenced here logically
(``ad_crm_id`` = EspoCRM record id, ``ad_number`` = CRM ``adnumbernews``). There
is no ``ad_purchases`` Postgres table. The endpoints that need to *read* a CRM ad
(``/candidates``, ``/unconfirmed``) use the read-only ``confirmed_ctl.crm.client``
adapter (see ``_lookup_crm_ad``); when the CRM is unconfigured they return 503.
"""
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from ..crm import client as crm_client
from ..db.models import AdConfirmation, BankTransaction, CrmAd, SyncLog
from ..db.session import get_db
from ..gmail.client import search_threads_by_ad_number
from ..matching.rag import store_confirmed_match
from ..matching.scorer import get_candidate_transactions

confirmed_ctl_bp = Blueprint("confirmed_ctl", __name__, url_prefix="/confirmed-ctl")


def _lookup_crm_ad(ad_crm_id: str) -> CrmAd | None:
    """Read a single CRM ad by its EspoCRM record id via the read-only adapter.

    Delegates to :func:`confirmed_ctl.crm.client.get_ad`, which reads from the
    MariaDB CRM (``permtrak2_crm.t_e_s_t_p_e_r_m``). Ad data is NEVER stored in
    this Postgres DB. Returns ``None`` when the CRM is unconfigured (callers
    should surface a 503) or when no row matches the id (callers 404).
    """
    return crm_client.get_ad(ad_crm_id)


@confirmed_ctl_bp.route("/sync", methods=["POST"])
def trigger_sync():
    """
    Called by the [Sync Now] button in the Flask UI.

    TODO(phase-later): wire this to the BofA email-scan / export ingestion
    adapters. The QuickBooks (QBO) sync backend was removed in Phase 1 when we
    pivoted away from the QBO API; the replacement ingestion adapters land in a
    later generation. Until then this endpoint is a no-op stub that reports it
    is not yet implemented (the request shape is preserved so the UI wiring can
    stay unchanged).
    """
    body = request.get_json(silent=True) or {}
    lookback = int(body.get("lookback_days", 2))

    return jsonify({
        "status": "not_implemented",
        "detail": (
            "Ingestion adapters (BofA email-scan / export) are not wired yet; "
            "the QBO sync backend was removed in Phase 1."
        ),
        "lookback_days": lookback,
    }), 501


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


@confirmed_ctl_bp.route("/candidates/<ad_crm_id>", methods=["GET"])
def get_candidates(ad_crm_id: str):
    """
    Called when user clicks an ad number in the UI.
    Returns ranked bank transaction candidates + Gmail thread results.

    ``ad_crm_id`` is the EspoCRM record id of the ad in the MariaDB CRM.
    """
    if not crm_client.is_configured():
        return jsonify({
            "status": "crm_not_configured",
            "detail": (
                "CRM not configured: set CRM_DB_HOST (and CRM_DB_USER/PASS/NAME) "
                "to enable the read-only permtrak2_crm.t_e_s_t_p_e_r_m adapter."
            ),
            "ad_crm_id": ad_crm_id,
        }), 503

    ad = _lookup_crm_ad(ad_crm_id)
    if ad is None:
        # CRM is configured but no row matches this EspoCRM record id.
        return jsonify({
            "status": "not_found",
            "detail": "No CRM ad found for the given id.",
            "ad_crm_id": ad_crm_id,
        }), 404

    with get_db() as db:
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
                "crm_id": ad.crm_id,
                "ad_number": ad.ad_number,
                "newspaper_name": ad.newspaper_name,
                "expected_amount": float(ad.expected_amount) if ad.expected_amount else None,
                "run_date": str(ad.run_date),
                "client_name": ad.client_name,
            },
            "bank_candidates": [
                {
                    "txn_id": c["transaction"].id,
                    "source": c["transaction"].source,
                    "source_txn_id": c["transaction"].source_txn_id,
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

    The ad is referenced logically: the client sends ``ad_crm_id`` (the EspoCRM
    record id) and ``ad_number`` (CRM ``adnumbernews``). No ad row is read from
    or written to this Postgres DB — ad data lives in the MariaDB CRM.
    """
    body = request.get_json()
    ad_crm_id = body.get("ad_crm_id")
    ad_number = body.get("ad_number")
    txn_id = body.get("bank_txn_id")       # internal DB id
    thread_id = body.get("gmail_thread_id")
    thread_subj = body.get("gmail_subject", "")
    confirmed_by = body.get("confirmed_by", "user")

    if not ad_crm_id:
        return jsonify({"error": "ad_crm_id is required"}), 400

    with get_db() as db:
        txn = db.get(BankTransaction, txn_id) if txn_id else None

        # Check for existing confirmation (idempotency) — keyed on the logical
        # CRM ad id (unique in ad_confirmations).
        existing = db.query(AdConfirmation).filter_by(ad_crm_id=ad_crm_id).first()
        if existing:
            return jsonify({"error": "Ad already confirmed", "confirmation_id": existing.id}), 409

        confirmed_at = datetime.now(timezone.utc)

        # Create confirmation record (logical ad reference — no FK to ad data).
        conf = AdConfirmation(
            ad_crm_id=ad_crm_id,
            ad_number=ad_number,
            bank_txn_id=txn.id if txn else None,
            gmail_thread_id=thread_id,
            gmail_subject=thread_subj,
            confirmed_by=confirmed_by,
            confirmed_at=confirmed_at,
            match_method="manual",
            match_confidence="manual",
        )
        db.add(conf)

        # Link transaction back to the ad via the logical pointer (no FK).
        if txn:
            txn.confirmed_ad_crm_id = ad_crm_id
            txn.confirmed_at = confirmed_at

        db.commit()

        # Store in RAG for future pattern learning (best-effort — never blocks confirm)
        if txn:
            try:
                store_confirmed_match(
                    ad_crm_id=ad_crm_id,
                    ad_number=ad_number or "",
                    # TODO(phase-later): pull newspaper_name / expected_amount from
                    # the CRM ad (t_e_s_t_p_e_r_m) once the read adapter lands; the
                    # confirm popup does not currently carry them.
                    newspaper_name=body.get("newspaper_name", ""),
                    expected_amount=float(body.get("expected_amount") or 0.0),
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
            "ad_crm_id": ad_crm_id,
            "ad_number": ad_number,
            "txn_source_txn_id": txn.source_txn_id if txn else None,
            "gmail_thread_id": thread_id,
        })


@confirmed_ctl_bp.route("/unconfirmed", methods=["GET"])
def list_unconfirmed():
    """
    Returns unconfirmed ads for the report table.

    Reads candidate ads from the MariaDB CRM via the read-only adapter
    (``crm.client.list_clearances`` — the verbatim ABCF-X clearances query) and
    subtracts the CRM ids already present in ``ad_confirmations.ad_crm_id``. Ad
    data is never stored in confirmed-ctl Postgres — only the logical
    ``ad_crm_id`` of already-confirmed ads is held here.
    """
    if not crm_client.is_configured():
        return jsonify({
            "status": "crm_not_configured",
            "detail": (
                "CRM not configured: set CRM_DB_HOST (and CRM_DB_USER/PASS/NAME) "
                "to enable the read-only permtrak2_crm.t_e_s_t_p_e_r_m adapter."
            ),
        }), 503

    clearances = crm_client.list_clearances()

    with get_db() as db:
        confirmed_ids = {
            row[0] for row in db.query(AdConfirmation.ad_crm_id).all()
        }

    unconfirmed = [ad for ad in clearances if ad.crm_id not in confirmed_ids]

    return jsonify({
        "count": len(unconfirmed),
        "ads": [
            {
                "crm_id": ad.crm_id,
                "ad_number": ad.ad_number,
                "client_name": ad.client_name,
                "newspaper_name": ad.newspaper_name,
                "run_date": str(ad.run_date) if ad.run_date else None,
                "expected_charge_date": (
                    str(ad.expected_charge_date) if ad.expected_charge_date else None
                ),
                "expected_amount": (
                    float(ad.expected_amount) if ad.expected_amount is not None else None
                ),
            }
            for ad in unconfirmed
        ],
    })
