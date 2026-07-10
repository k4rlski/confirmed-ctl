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
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from flask import Blueprint, jsonify, request

from .. import settings
from ..crm import client as crm_client
from ..db.models import AdConfirmation, BankTransaction, CrmAd, SyncLog
from ..db.session import get_db
from ..gmail.client import search_threads_by_ad_number
from ..matching.rag import store_confirmed_match
from ..matching.scorer import get_candidate_transactions, get_excluded_transactions

logger = logging.getLogger(__name__)

confirmed_ctl_bp = Blueprint("confirmed_ctl", __name__, url_prefix="/confirmed-ctl")


def _crm_ad_to_dict(ad: CrmAd) -> dict:
    """Serialize a :class:`CrmAd` to the JSON contract shared by the endpoints.

    Dates are ``str()``/``None``; strings are None-safe. ``status_news`` and
    ``clearance_status`` are raw EspoCRM enum strings passed through as-is. This
    mirrors the inline serialization used by ``/unconfirmed`` and ``/candidates``
    (kept identical key-for-key) and is the base payload for ``/reconciled``.
    """
    return {
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
        "case_number": str(ad.case_number) if ad.case_number is not None else None,
        "state": str(ad.state) if ad.state is not None else None,
        "attorney": str(ad.attorney) if ad.attorney is not None else None,
        "entity": str(ad.entity) if ad.entity is not None else None,
        "job_title": str(ad.job_title) if ad.job_title is not None else None,
        "run_end": str(ad.run_end) if ad.run_end else None,
        "status_news": str(ad.status_news) if ad.status_news is not None else None,
        "owner": str(ad.owner) if ad.owner is not None else None,
        "approved_date": str(ad.approved_date) if ad.approved_date else None,
        "buy_date": str(ad.buy_date) if ad.buy_date else None,
        "beneficiary_first": (
            str(ad.beneficiary_first) if ad.beneficiary_first is not None else None
        ),
        "beneficiary_last": (
            str(ad.beneficiary_last) if ad.beneficiary_last is not None else None
        ),
        "clearance_status": (
            str(ad.clearance_status) if ad.clearance_status is not None else None
        ),
    }


def _lookup_crm_ad(ad_crm_id: str) -> CrmAd | None:
    """Read a single CRM ad by its EspoCRM record id via the read-only adapter.

    Delegates to :func:`confirmed_ctl.crm.client.get_ad`, which reads from the
    MariaDB CRM (``permtrak2_crm.t_e_s_t_p_e_r_m``). Ad data is NEVER stored in
    this Postgres DB. Returns ``None`` when the CRM is unconfigured (callers
    should surface a 503) or when no row matches the id (callers 404).
    """
    return crm_client.get_ad(ad_crm_id)


def _coerce_raw_json(raw) -> dict:
    """Return a ``bank_transactions.raw_json`` value as a plain dict.

    The column is JSONB (already a ``dict`` when read via SQLAlchemy) but an
    ingestion adapter or a manual row may have stored it as a JSON *string*.
    Handle both: parse a ``str`` (returning ``{}`` if it is not a JSON object)
    and pass a ``dict`` through unchanged. Anything else (``None``/list/number)
    yields ``{}`` so callers can always ``.get()`` safely.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


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

    Format (account-index-agnostic)::

        https://mail.google.com/mail/?authuser={GMAIL_IMPERSONATE}#all/{gmail_thread_id}

    Using ``?authuser=<email>`` + ``#all/<thread_id>`` opens the exact thread
    regardless of which Google account slot (``/u/0``, ``/u/1``, …) the viewer
    happens to be signed into — the old ``/u/1/#search/{adnum}`` form broke when
    the account index differed. ``ad_number`` is retained in the signature for
    backward compatibility but is no longer part of the URL. Returns an empty
    string when no thread id was selected — the caller still writes the other
    fields but logs the omission.
    """
    if not gmail_thread_id:
        return ""
    return (
        f"https://mail.google.com/mail/?authuser={settings.GMAIL_IMPERSONATE}"
        f"#all/{gmail_thread_id}"
    )


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
        # Bank transaction candidates (ranked).
        #
        # CONSUMED-EXCLUSION INVARIANT: get_candidate_transactions() only returns
        # UNCONSUMED, non-ignored rows — its query filters
        # ``confirmed_ad_crm_id IS NULL`` AND ``ignored IS FALSE`` (see
        # confirmed_ctl/matching/scorer.py, the two .filter() lines
        # ``BankTransaction.confirmed_ad_crm_id.is_(None)`` and
        # ``BankTransaction.ignored.is_(False)``). So once a bank txn is mapped to
        # an ad (confirmed_ad_crm_id set at /confirm) or flagged as SAAS/vendor
        # noise, it can NEVER reappear as a candidate for another ad's
        # reconciliation. Do not relax this filter.
        candidates = get_candidate_transactions(db, ad)

        # Near-miss bank txns EXCLUDED from the candidate set (bounded <=10):
        # plausible by amount but out-of-window or already matched. Surfaced so
        # operators can spot e.g. a second identical charge just outside the
        # window. Best-effort — never blocks the popup.
        try:
            excluded = get_excluded_transactions(db, ad)
        except Exception:
            logger.exception(
                "excluded-txn lookup failed for ad_crm_id=%s", ad_crm_id
            )
            excluded = []

        # Gmail threads. Blank ad number => a distinguishable "note" (we did NOT
        # search); a real search failure => a surfaced "gmail_error" (we do NOT
        # silently pretend there were no results). Otherwise the ranked thread
        # summaries (each with gmail_url + matched_by).
        gmail_threads: list[dict] = []
        gmail_error: str | None = None
        gmail_note: str | None = None
        if not (ad.ad_number or "").strip():
            gmail_note = "No ad number on record"
        else:
            try:
                gmail_threads = search_threads_by_ad_number(
                    ad.ad_number,
                    newspaper_name=ad.newspaper_name,
                    charge_date=ad.expected_charge_date,
                )
            except ValueError as exc:
                # Blank/whitespace ad number guarded inside the client.
                logger.warning(
                    "Gmail search skipped for ad_crm_id=%s: %s", ad_crm_id, exc
                )
                gmail_note = "No ad number on record"
            except Exception:
                # Real search failure (auth, API, network). Log server-side and
                # surface a controlled error instead of an empty result set.
                logger.exception(
                    "Gmail thread search failed for ad_crm_id=%s", ad_crm_id
                )
                gmail_error = "Gmail search failed; threads could not be loaded."

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
                # Richer ad-identifying fields (ABCF-X columns), None-safe strings.
                "case_number": str(ad.case_number) if ad.case_number is not None else None,
                "state": str(ad.state) if ad.state is not None else None,
                "attorney": str(ad.attorney) if ad.attorney is not None else None,
                "entity": str(ad.entity) if ad.entity is not None else None,
                # Additional ABCF-X reconcile columns. run_end is a date; the
                # others are None-safe strings (status_news is the raw statnews
                # enum string passed through as-is).
                "job_title": str(ad.job_title) if ad.job_title is not None else None,
                "run_end": str(ad.run_end) if ad.run_end else None,
                "status_news": str(ad.status_news) if ad.status_news is not None else None,
                "owner": str(ad.owner) if ad.owner is not None else None,
                # Additional ABCF-X contract columns. approved_date/buy_date are
                # dates; beneficiary_last is a None-safe string; clearance_status
                # is the raw statclearancenews enum string passed through as-is.
                "approved_date": str(ad.approved_date) if ad.approved_date else None,
                "buy_date": str(ad.buy_date) if ad.buy_date else None,
                "beneficiary_first": (
                    str(ad.beneficiary_first) if ad.beneficiary_first is not None else None
                ),
                "beneficiary_last": (
                    str(ad.beneficiary_last) if ad.beneficiary_last is not None else None
                ),
                "clearance_status": (
                    str(ad.clearance_status) if ad.clearance_status is not None else None
                ),
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
            "gmail_error": gmail_error,
            "gmail_note": gmail_note,
            "excluded": excluded,
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

        # --- CRM write-back (verified 3-field allowlist, gated) ---------------
        # Assemble the exact write values from the matched bank transaction /
        # request, then (only when the gate is on and there is a matched txn)
        # write them to the CRM BEFORE committing the Postgres confirmation, so a
        # CRM failure leaves nothing persisted and the confirm is cleanly
        # retryable. The staff-owned datepaidnews column is never written here.
        trxstring = _build_trxstring(txn) if txn else ""
        gmail_url = _build_gmail_url(ad_number, thread_id)

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
                    "self-healing) trxstring=%r urlgmailadconfirm=%r",
                    ad_crm_id,
                    trxstring,
                    gmail_url,
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
                # Richer ad-identifying fields (ABCF-X columns), None-safe strings.
                "case_number": str(ad.case_number) if ad.case_number is not None else None,
                "state": str(ad.state) if ad.state is not None else None,
                "attorney": str(ad.attorney) if ad.attorney is not None else None,
                "entity": str(ad.entity) if ad.entity is not None else None,
                # Additional ABCF-X reconcile columns. run_end is a date; the
                # others are None-safe strings (status_news is the raw statnews
                # enum string passed through as-is).
                "job_title": str(ad.job_title) if ad.job_title is not None else None,
                "run_end": str(ad.run_end) if ad.run_end else None,
                "status_news": str(ad.status_news) if ad.status_news is not None else None,
                "owner": str(ad.owner) if ad.owner is not None else None,
                # Additional ABCF-X contract columns. approved_date/buy_date are
                # dates; beneficiary_last is a None-safe string; clearance_status
                # is the raw statclearancenews enum string passed through as-is.
                "approved_date": str(ad.approved_date) if ad.approved_date else None,
                "buy_date": str(ad.buy_date) if ad.buy_date else None,
                "beneficiary_first": (
                    str(ad.beneficiary_first) if ad.beneficiary_first is not None else None
                ),
                "beneficiary_last": (
                    str(ad.beneficiary_last) if ad.beneficiary_last is not None else None
                ),
                "clearance_status": (
                    str(ad.clearance_status) if ad.clearance_status is not None else None
                ),
            }
            for ad in unconfirmed
        ],
    })


@confirmed_ctl_bp.route("/reconciled", methods=["GET"])
def list_reconciled():
    """
    Returns ads this tool has already reconciled (marked clearance Done), with
    the bank txn + Gmail info it mapped, for the reconcile page's "done" view.

    Reads the Done ads from the MariaDB CRM via the read-only adapter
    (``crm.client.list_reconciled`` — the ABCF-X query with
    ``statclearancenews='["Done"]'``) and JOINs each to its Postgres
    ``ad_confirmations`` row (by ``ad_crm_id`` = ``CrmAd.crm_id``). Only ads that
    HAVE an ``ad_confirmations`` row (i.e. actually reconciled by THIS tool) are
    returned. Each includes the mapped bank amount + txn date (via
    ``ad_confirmations.bank_txn_id`` -> ``bank_transactions``), ``gmail_thread_id``,
    a ``gmail_url`` (authuser ``#all`` form), and ``confirmed_at``. Ordered by
    ``confirmed_at`` DESC. None-safe throughout.
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
        reconciled_ads = crm_client.list_reconciled()
    except Exception:
        # Same controlled-failure contract as /unconfirmed & /candidates.
        logger.exception("CRM list_reconciled failed")
        return jsonify({
            "status": "crm_unavailable",
            "detail": "CRM lookup failed; the CRM is unreachable or misconfigured.",
        }), 502

    crm_ids = [ad.crm_id for ad in reconciled_ads if ad.crm_id]

    ads_out: list[dict] = []
    with get_db() as db:
        # Map ad_crm_id -> its confirmation row (only ads reconciled by us).
        conf_by_id: dict[str, AdConfirmation] = {}
        if crm_ids:
            for conf in (
                db.query(AdConfirmation)
                .filter(AdConfirmation.ad_crm_id.in_(crm_ids))
                .all()
            ):
                conf_by_id[conf.ad_crm_id] = conf

        # Map bank_txn_id -> bank txn for the mapped amount + date.
        txn_ids = [c.bank_txn_id for c in conf_by_id.values() if c.bank_txn_id]
        txn_by_id: dict[int, BankTransaction] = {}
        if txn_ids:
            for txn in (
                db.query(BankTransaction)
                .filter(BankTransaction.id.in_(txn_ids))
                .all()
            ):
                txn_by_id[txn.id] = txn

        for ad in reconciled_ads:
            conf = conf_by_id.get(ad.crm_id) if ad.crm_id else None
            if conf is None:
                # Not reconciled by this tool (no local confirmation) — skip.
                continue
            txn = txn_by_id.get(conf.bank_txn_id) if conf.bank_txn_id else None
            item = _crm_ad_to_dict(ad)
            item.update({
                # Joined bank transaction id so the frontend can deep-link the
                # read-only Bank-Trx modal (GET /bank-transaction/<id>). None-safe
                # when this reconciled ad has no mapped bank txn.
                "bank_txn_id": txn.id if txn is not None else None,
                "bank_amount": (
                    float(txn.total_amount)
                    if txn is not None and txn.total_amount is not None
                    else None
                ),
                "bank_txn_date": (
                    str(txn.txn_date) if txn is not None and txn.txn_date else None
                ),
                "gmail_thread_id": conf.gmail_thread_id,
                "gmail_url": _build_gmail_url(ad.ad_number, conf.gmail_thread_id),
                # BofA transaction-alert Gmail deep link for the mapped bank txn
                # (top-level column on bank_transactions). Empty string when this
                # reconciled ad has no mapped bank txn or the txn predates the
                # bofa_gmail_thread_id capture. NEVER the ad-confirm thread above.
                "bofa_gmail_url": (
                    _build_gmail_url(None, txn.bofa_gmail_thread_id)
                    if txn is not None
                    else ""
                ),
                "confirmed_at": (
                    conf.confirmed_at.isoformat() if conf.confirmed_at else None
                ),
            })
            ads_out.append(item)

    # Order by confirmed_at DESC. Sorting on the ISO string avoids naive/aware
    # datetime comparison issues; missing timestamps sort last.
    ads_out.sort(key=lambda d: d.get("confirmed_at") or "", reverse=True)

    return jsonify({"count": len(ads_out), "ads": ads_out})


@confirmed_ctl_bp.route("/bank-transaction/<txn_id>", methods=["GET"])
def get_bank_transaction(txn_id: str):
    """Read-only detail for a single bank transaction (Bank-Trx modal).

    Looks up ``bank_transactions`` by primary key (``id``) and returns the
    human-facing fields the confirmed-ctl-adm Bank-Trx modal renders. This is
    ADDITIVE and READ-ONLY — it never mutates state and never touches the CRM
    write path.

    Field notes:

    - ``amount`` is the raw signed ``total_amount`` (debits are stored NEGATIVE);
      it is returned as-is so the UI can format the sign/currency.
    - ``merchant_raw`` / ``merchant`` (the "vendor trx string", i.e. the raw bank
      memo) live in the row's ``raw_json`` blob, NOT as top-level columns, and so
      does ``posted_date``. ``raw_json`` may be a dict (JSONB) or a JSON string —
      both are handled via :func:`_coerce_raw_json`.
    - ``line_descriptions`` prefers ``raw_json['line_descriptions']`` and falls
      back to the ``line_descriptions`` ARRAY column.
    - Dates serialize None-safely (``str()`` for dates, ``isoformat()`` for
      timestamps, else ``None``).

    Related CRM summary: when ``confirmed_ad_crm_id`` is set the endpoint calls
    the existing read-only CRM reader (:func:`_lookup_crm_ad` -> ``get_ad``) and
    includes ``related: {crm_id, case_number, client_name, ad_number,
    newspaper_name}``. When the txn is unconsumed ``related`` is ``null``. If the
    OPTIONAL CRM lookup raises (CRM unreachable/misconfigured) the txn detail is
    STILL returned with ``related=null`` plus ``related_error:"crm_unavailable"``
    — the modal must always render, so a CRM outage never 502s this endpoint.

    Returns 404 ``{error:"not_found"}`` for an unknown ``txn_id``.
    """
    with get_db() as db:
        txn = db.get(BankTransaction, txn_id)
        if txn is None:
            return jsonify({"error": "not_found"}), 404

        raw = _coerce_raw_json(txn.raw_json)
        line_descriptions = raw.get("line_descriptions")
        if line_descriptions is None:
            line_descriptions = txn.line_descriptions

        payload = {
            "txn_id": txn.id,
            "amount": (
                float(txn.total_amount) if txn.total_amount is not None else None
            ),
            "vendor_name": txn.vendor_name,
            "merchant_raw": raw.get("merchant_raw"),
            "merchant": raw.get("merchant"),
            "line_descriptions": line_descriptions,
            "txn_date": str(txn.txn_date) if txn.txn_date else None,
            "posted_date": raw.get("posted_date"),
            "source": txn.source,
            "source_txn_id": txn.source_txn_id,
            "ignored": txn.ignored,
            "ignore_reason": txn.ignore_reason,
            "confirmed_at": (
                txn.confirmed_at.isoformat() if txn.confirmed_at else None
            ),
            "created_at": (
                txn.created_in_db.isoformat() if txn.created_in_db else None
            ),
            "confirmed_ad_crm_id": txn.confirmed_ad_crm_id,
            # BofA transaction-alert Gmail thread that produced this row (captured
            # at ingest). ``bofa_gmail_url`` is the account-index-agnostic deep
            # link built on read (empty string when no thread id). This is the
            # bank-alert email — distinct from the ad-confirmation thread that
            # lands under ``related.ad_confirm_gmail_*``.
            "bofa_gmail_thread_id": txn.bofa_gmail_thread_id,
            "bofa_gmail_url": _build_gmail_url(None, txn.bofa_gmail_thread_id),
            "related": None,
        }

        # Optional related-CRM summary. Only for a CONSUMED txn (a logical CRM ad
        # pointer is set). A CRM outage must NOT sink the whole endpoint — the
        # txn detail always renders; we degrade to related=null + related_error.
        if txn.confirmed_ad_crm_id:
            # Ad-confirmation Gmail thread for this ad (from ad_confirmations —
            # the vendor's ad-confirmation email, NOT the BofA alert above). Read
            # from the same-DB Postgres row; None-safe when absent. This read is
            # cheap and never blocks the modal.
            ad_conf = (
                db.query(AdConfirmation)
                .filter(AdConfirmation.ad_crm_id == txn.confirmed_ad_crm_id)
                .first()
            )
            ad_confirm_thread_id = ad_conf.gmail_thread_id if ad_conf else None
            try:
                ad = _lookup_crm_ad(txn.confirmed_ad_crm_id)
            except Exception:
                logger.exception(
                    "related-CRM lookup failed for bank txn id=%s (ad_crm_id=%s)",
                    txn.id,
                    txn.confirmed_ad_crm_id,
                )
                payload["related_error"] = "crm_unavailable"
            else:
                if ad is not None:
                    payload["related"] = {
                        "crm_id": ad.crm_id,
                        "case_number": (
                            str(ad.case_number)
                            if ad.case_number is not None
                            else None
                        ),
                        "client_name": ad.client_name,
                        "ad_number": ad.ad_number,
                        "newspaper_name": ad.newspaper_name,
                        # Widened Related-CRM fields for the Bank-Trx modal.
                        # None-safe strings; run_date (datenewsstart) / run_end
                        # (datenewsend) serialize as date strings.
                        "job_title": (
                            str(ad.job_title) if ad.job_title is not None else None
                        ),
                        "beneficiary_first": (
                            str(ad.beneficiary_first)
                            if ad.beneficiary_first is not None
                            else None
                        ),
                        "beneficiary_last": (
                            str(ad.beneficiary_last)
                            if ad.beneficiary_last is not None
                            else None
                        ),
                        "attorney": (
                            str(ad.attorney) if ad.attorney is not None else None
                        ),
                        "run_date": str(ad.run_date) if ad.run_date else None,
                        "run_end": str(ad.run_end) if ad.run_end else None,
                        # Ad-confirmation Gmail thread + deep link (distinct from
                        # the top-level BofA alert thread). URL is empty when no
                        # ad-confirmation thread was recorded.
                        "ad_confirm_gmail_thread_id": ad_confirm_thread_id,
                        "ad_confirm_gmail_url": _build_gmail_url(
                            ad.ad_number, ad_confirm_thread_id
                        ),
                    }

        return jsonify(payload)
