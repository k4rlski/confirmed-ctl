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
import logging
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from flask import Blueprint, jsonify, request

from .. import settings
from ..crm import client as crm_client
from ..db.models import AdConfirmation, BankTransaction, CrmAd, SyncLog
from ..db.session import get_db
from ..gmail.client import search_threads_by_ad_number
from ..matching.rag import store_confirmed_match
from ..matching.scorer import get_candidate_transactions

logger = logging.getLogger(__name__)

confirmed_ctl_bp = Blueprint("confirmed_ctl", __name__, url_prefix="/confirmed-ctl")


def _lookup_crm_ad(ad_crm_id: str) -> CrmAd | None:
    """Read a single CRM ad by its EspoCRM record id via the read-only adapter.

    Delegates to :func:`confirmed_ctl.crm.client.get_ad`, which reads from the
    MariaDB CRM (``permtrak2_crm.t_e_s_t_p_e_r_m``). Ad data is NEVER stored in
    this Postgres DB. Returns ``None`` when the CRM is unconfigured (callers
    should surface a 503) or when no row matches the id (callers 404).
    """
    return crm_client.get_ad(ad_crm_id)


# --------------------------------------------------------------------------- #
# CRM write-back value builders (see confirmed_ctl.crm.client.update_ad_clearance)
#
# These assemble the exact VERIFIED write formats reproduced from a completed CRM
# record. They are plain text fields, easily tuned later.
# --------------------------------------------------------------------------- #
def _format_signed_amount(amount) -> str:
    """Format a signed dollar amount like ``-$2,226.94`` (debits lead with '-').

    Thousands comma, exactly 2 decimals, leading ``$``; a leading minus for
    negative (debit) amounts. Bank alerts store debits as negative amounts.
    Returns an empty string when the amount is missing/unparseable.
    """
    if amount is None:
        return ""
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError):
        return ""
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def _build_trxstring(txn: BankTransaction) -> str:
    """Assemble the CRM ``trxstring`` from a matched bank transaction.

    Verified reference literal (from completed record 6a343d2127bb55b5a) was::

        CHECKCARD LA TIMES MEDIA GR EL SEGUNDO CA ON 06/26 Debit\\t-$2,226.94

    i.e. a rich memo/description composite, a literal TAB, then the signed
    amount. The email-scan adapter does not carry BofA's verbatim memo, so we
    build the richest composite available from the model:

        {payment_type} {vendor_name} ON MM/DD {Debit|Credit}\\t{signed amount}

    Non-empty parts only; the amount sign drives the Debit/Credit word and the
    ``-$`` sign. This is a plain TEXT field — tune the template freely later.
    """
    parts: list[str] = []
    if txn.payment_type:
        parts.append(str(txn.payment_type).strip())
    if txn.vendor_name:
        parts.append(str(txn.vendor_name).strip())
    if txn.txn_date:
        parts.append(f"ON {txn.txn_date:%m/%d}")
    if txn.total_amount is not None:
        try:
            direction = "Debit" if Decimal(str(txn.total_amount)) < 0 else "Credit"
            parts.append(direction)
        except (InvalidOperation, ValueError, TypeError):
            pass
    memo = " ".join(p for p in parts if p)
    return f"{memo}\t{_format_signed_amount(txn.total_amount)}"


def _build_gmail_url(ad_number: str | None, gmail_thread_id: str | None) -> str:
    """Build the CRM ``urlgmailadconfirm`` Gmail deep link.

    Format (verified)::

        https://mail.google.com/mail/u/1/#search/{adnumber}/{gmail_thread_id}

    ``adnumber`` is the ad number ``.strip()``ed (CRM ``adnumbernews`` carries
    trailing spaces). Returns an empty string when no thread id was selected —
    the caller still writes the other fields but logs the omission.
    """
    if not gmail_thread_id:
        return ""
    adnum = (ad_number or "").strip()
    return f"https://mail.google.com/mail/u/1/#search/{adnum}/{gmail_thread_id}"


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

    try:
        ad = _lookup_crm_ad(ad_crm_id)
    except Exception:
        # A configured-but-unreachable CRM (outage, wrong creds, Remote-MySQL
        # allowlist not granted) raises pymysql errors here (a subclass of
        # Exception). Log server-side and return a controlled 502 instead of an
        # unhandled 500 that would leak a stack trace to the client.
        logger.exception("CRM lookup failed for ad_crm_id=%s", ad_crm_id)
        return jsonify({
            "status": "crm_unavailable",
            "detail": "CRM lookup failed; the CRM is unreachable or misconfigured.",
            "ad_crm_id": ad_crm_id,
        }), 502

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
                "expected_amount": (
                    float(ad.expected_amount) if ad.expected_amount is not None else None
                ),
                "run_date": str(ad.run_date) if ad.run_date else None,
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

        # --- CRM write-back (verified 4-field allowlist, gated) ---------------
        # Assemble the exact write values from the matched bank transaction /
        # request, then (only when the gate is on and there is a matched txn)
        # write them to the CRM BEFORE committing the Postgres confirmation, so a
        # CRM failure leaves nothing persisted and the confirm is cleanly
        # retryable.
        trxstring = _build_trxstring(txn) if txn else ""
        gmail_url = _build_gmail_url(ad_number, thread_id)
        datepaid = (
            txn.txn_date.strftime("%Y-%m-%d")
            if txn and isinstance(txn.txn_date, date)
            else ""
        )

        if settings.CRM_WRITE_ENABLED:
            if txn and ad_crm_id:
                if not thread_id:
                    logger.warning(
                        "confirm: no gmail_thread_id for ad_crm_id=%s; "
                        "writing empty urlgmailadconfirm",
                        ad_crm_id,
                    )
                try:
                    crm_client.update_ad_clearance(
                        ad_crm_id=ad_crm_id,
                        trxstring=trxstring,
                        urlgmailadconfirm=gmail_url,
                        datepaid=datepaid,
                    )
                    crm_write = "written"
                except crm_client.CrmWriteDisabled:
                    # Gate flipped off between check and call — treat as disabled.
                    crm_write = "disabled"
                except crm_client.CrmWriteError:
                    # The UPDATE matched NO CRM row for this ad_crm_id (bad/stale
                    # id) — the write never landed. Do NOT commit the Postgres
                    # confirmation; roll back and return a controlled 502 so the
                    # confirm can be retried once the id is corrected.
                    logger.error(
                        "CRM write-back matched no row for ad_crm_id=%s", ad_crm_id
                    )
                    db.rollback()
                    return jsonify({
                        "status": "crm_write_failed",
                        "detail": "no CRM row matched ad_crm_id",
                        "ad_crm_id": ad_crm_id,
                    }), 502
                except Exception:
                    # A live CRM UPDATE failure (pymysql error). Do NOT commit the
                    # Postgres confirmation — roll back and return a controlled 502
                    # so the confirm can be retried cleanly.
                    logger.exception(
                        "CRM write-back failed for ad_crm_id=%s", ad_crm_id
                    )
                    db.rollback()
                    return jsonify({
                        "status": "crm_write_failed",
                        "detail": (
                            "CRM write-back failed; the CRM is unreachable or "
                            "rejected the update. No confirmation was saved — retry."
                        ),
                        "ad_crm_id": ad_crm_id,
                    }), 502
            else:
                # Enabled but nothing to write against (no matched bank txn).
                crm_write = "skipped_no_txn"
        else:
            crm_write = "disabled"

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

        if crm_write == "written":
            # Reverse-orphan guard: the CRM row is ALREADY marked Done. If the
            # Postgres commit now fails we have a CRM write with no local record.
            # Log CRITICAL with the written values so it can be reconciled, and
            # return a controlled 500. Retry is safe: no AdConfirmation exists (so
            # no 409) and the CRM re-write is idempotent (FOUND_ROWS => rowcount
            # 1 on the unchanged row).
            try:
                db.commit()
            except Exception:
                logger.critical(
                    "CRM written (statclearancenews=Done) but Postgres commit "
                    "FAILED — reconcile ad_crm_id=%s (retry is idempotent/"
                    "self-healing) trxstring=%r urlgmailadconfirm=%r "
                    "datepaidnews=%r",
                    ad_crm_id,
                    trxstring,
                    gmail_url,
                    datepaid,
                )
                return jsonify({
                    "status": "postgres_commit_failed_after_crm_write",
                    "detail": (
                        "CRM updated but local confirmation not saved; retry to "
                        "reconcile"
                    ),
                    "ad_crm_id": ad_crm_id,
                }), 500
        else:
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
            # CRM write-back outcome + the exact values written (so the UI can
            # display them). "written" / "disabled" / "skipped_no_txn".
            "crm_write": crm_write,
            "crm_values": {
                "statclearancenews": "Done",
                "trxstring": trxstring,
                "urlgmailadconfirm": gmail_url,
                "datepaidnews": datepaid,
            },
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

    try:
        clearances = crm_client.list_clearances()
    except Exception:
        # Same controlled-failure contract as /candidates: a configured-but-
        # unreachable CRM raises pymysql errors (subclass of Exception); log and
        # return 502 rather than an unhandled 500 with a leaked stack trace.
        logger.exception("CRM list_clearances failed")
        return jsonify({
            "status": "crm_unavailable",
            "detail": "CRM lookup failed; the CRM is unreachable or misconfigured.",
        }), 502

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
