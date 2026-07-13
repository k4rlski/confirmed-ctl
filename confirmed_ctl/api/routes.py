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

from .. import settings, vendors
from ..crm import client as crm_client
from ..db.models import (
    AdConfirmation,
    AdRep,
    AdRepMerchantLink,
    BankMerchantString,
    BankTransaction,
    CrmAd,
    SyncLog,
)
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
        # Gmail threads FIRST — so the ad-confirmation From addresses are known
        # before scoring (they drive the rep-email vendor-link path). Blank ad
        # number => a distinguishable "note" (we did NOT search); a real search
        # failure => a surfaced "gmail_error" (we do NOT silently pretend there
        # were no results). Otherwise the ranked thread summaries (each with
        # gmail_url + matched_by).
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

        # The set of ad-confirmation From email addresses (rep-email path): parse
        # each thread's raw From header to a bare address. Used only to LIFT a
        # linked candidate — never to exclude anything.
        from_emails: set[str] = set()
        for th in gmail_threads:
            _d, em, _dom = vendors.parse_email_header(th.get("from"))
            if em:
                from_emails.add(em)

        # Read-once vendor-link index (rep<->merchant-string registry). Passed to
        # the scorer so a candidate whose merchant string is a known/linked ad
        # vendor is nudged up (with match_reasons recorded). Best-effort — a
        # registry read failure must never sink the popup.
        try:
            link_index = vendors.build_vendor_link_index(db)
        except Exception:
            logger.exception(
                "vendor-link index build failed for ad_crm_id=%s", ad_crm_id
            )
            link_index = None

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
        candidates = get_candidate_transactions(
            db, ad, link_index=link_index, from_emails=from_emails
        )

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
                    # Vendor-link transparency: why (if at all) this candidate was
                    # boosted, and by how much (0.0 when no link matched).
                    "match_reasons": c.get("match_reasons", []),
                    "boost_delta": c.get("boost_delta", 0.0),
                    "base_score_pct": int(c.get("base_score", c["score"]) * 100),
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
                # Receipt PDF pulled from the ad-confirmation thread (receipt-ctl
                # confirmed-ctl half). ``receipt_file_path`` is the fang-local
                # path; ``has_receipt`` is the UI indicator. A web-served download
                # endpoint is a follow-up (files live on fang disk, not web-root).
                "receipt_file_path": conf.receipt_file_path,
                "has_receipt": bool(conf.receipt_file_path),
            })
            ads_out.append(item)

    # Order by confirmed_at DESC. Sorting on the ISO string avoids naive/aware
    # datetime comparison issues; missing timestamps sort last.
    ads_out.sort(key=lambda d: d.get("confirmed_at") or "", reverse=True)

    return jsonify({"count": len(ads_out), "ads": ads_out})


@confirmed_ctl_bp.route("/suggested", methods=["GET"])
def list_suggested():
    """Return high-confidence, NOT-yet-confirmed ad <-> bank txn suggestions.

    This is the bulk, precomputed companion to ``/candidates/<ad_crm_id>``: it
    scores EVERY unconfirmed clearance ad (the same set ``/unconfirmed`` returns)
    against the unmatched bank transactions and surfaces the best bank pair per
    ad whose score clears ``min_score``. It deliberately does NOT do the Gmail
    thread search that ``/candidates`` does — this endpoint is a cheap ranking
    over local Postgres bank rows so the reconcile page can show an at-a-glance
    "these look ready to map" list without N per-ad round-trips (and N Gmail
    API calls).

    READ-ONLY: it never writes to Postgres or the CRM and creates NO new tables.
    The actual mapping still happens through the existing Map Trx modal ->
    ``/confirm`` flow; a suggestion is only a ranked hint, never an auto-confirm.

    Query params:
      - ``min_score`` (float, default 0.6, clamped to [0, 1]): the minimum scorer
        score for a pair to be suggested. A strong amount + date match scores
        ~0.6 even without a vendor-name match, so 0.6 is the "worth a look"
        floor; the UI can request a higher bar.
      - ``limit`` (int, default 200): cap on suggestions returned.

    Same CRM-availability contract as ``/unconfirmed`` (503 not configured / 502
    unreachable). Each suggestion carries the ad identity (via the shared
    ``_crm_ad_to_dict`` serializer) plus the suggested bank txn's identifying
    fields, the score, and ``alt_count`` = how many OTHER bank txns also cleared
    ``min_score`` for that ad (so the operator can see when a suggestion is
    ambiguous). Sorted by score DESC.
    """
    if not crm_client.is_configured():
        return jsonify({
            "status": "crm_not_configured",
            "detail": (
                "CRM not configured: set CRM_DB_HOST (and CRM_DB_USER/PASS/NAME) "
                "to enable the read-only permtrak2_crm.t_e_s_t_p_e_r_m adapter."
            ),
        }), 503

    # Parse + clamp query params (robust to junk input).
    try:
        min_score = float(request.args.get("min_score", 0.6))
    except (TypeError, ValueError):
        min_score = 0.6
    min_score = max(0.0, min(1.0, min_score))
    try:
        limit = int(request.args.get("limit", 200))
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(1000, limit))

    try:
        clearances = crm_client.list_clearances()
    except Exception:
        logger.exception("CRM list_clearances failed (suggested)")
        return jsonify({
            "status": "crm_unavailable",
            "detail": "CRM lookup failed; the CRM is unreachable or misconfigured.",
        }), 502

    suggestions: list[dict] = []
    with get_db() as db:
        confirmed_ids = {
            row[0] for row in db.query(AdConfirmation.ad_crm_id).all()
        }
        unconfirmed = [ad for ad in clearances if ad.crm_id not in confirmed_ids]

        # Read-once vendor-link index shared across every ad's scoring. No Gmail
        # per-ad here (that is the /candidates path), so there are no From
        # addresses — the boost uses the bank-string link path only.
        try:
            link_index = vendors.build_vendor_link_index(db)
        except Exception:
            logger.exception("vendor-link index build failed (suggested)")
            link_index = None

        for ad in unconfirmed:
            # Reuse the SAME scorer + consumed/ignored-exclusion invariant as the
            # per-ad popup. top_n is small; we only need the best + a count of
            # other qualifying candidates for the ambiguity hint.
            try:
                scored = get_candidate_transactions(
                    db, ad, top_n=8, link_index=link_index
                )
            except Exception:
                logger.exception(
                    "suggested scoring failed for ad_crm_id=%s", ad.crm_id
                )
                continue
            qualifying = [c for c in scored if c["score"] >= min_score]
            if not qualifying:
                continue
            best = qualifying[0]  # get_candidate_transactions already sorts DESC
            txn = best["transaction"]
            item = _crm_ad_to_dict(ad)
            item["suggested_txn"] = {
                "txn_id": txn.id,
                "source": txn.source,
                "source_txn_id": txn.source_txn_id,
                "txn_date": str(txn.txn_date) if txn.txn_date else None,
                "amount": (
                    float(txn.total_amount) if txn.total_amount is not None else None
                ),
                "vendor_name": txn.vendor_name,
                "account_name": txn.account_name,
                "payment_ref": txn.payment_ref_num,
                "memo": txn.private_note,
            }
            item["score"] = round(best["score"], 3)
            item["score_pct"] = int(best["score"] * 100)
            # Vendor-link transparency for the best pair (empty / 0.0 when the
            # merchant string is not catalogued or linked).
            item["match_reasons"] = best.get("match_reasons", [])
            item["boost_delta"] = best.get("boost_delta", 0.0)
            item["base_score_pct"] = int(best.get("base_score", best["score"]) * 100)
            # Other bank txns that also cleared min_score for this ad (ambiguity).
            item["alt_count"] = len(qualifying) - 1
            suggestions.append(item)

    suggestions.sort(key=lambda d: d.get("score") or 0.0, reverse=True)
    suggestions = suggestions[:limit]

    return jsonify({
        "count": len(suggestions),
        "min_score": min_score,
        "suggestions": suggestions,
    })


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


# =========================================================================== #
# Ad-rep <-> bank merchant-string registry (vendor-map)
#
# CRUD over three standalone-Postgres tables (ad_reps / bank_merchant_strings /
# ad_rep_merchant_links). NEVER touches the CRM. Delete policy: HARD delete for
# all three (a rep/string delete cascades to its links via the FK). The UI
# confirms every delete. See confirmed_ctl.vendors for normalization/upsert.
# =========================================================================== #
def _vendor_body() -> dict:
    return request.get_json(silent=True) or {}


@confirmed_ctl_bp.route("/vendor-map", methods=["GET"])
def vendor_map_overview():
    """Combined registry view for the MARS page.

    Returns every rep (each with its linked merchant strings), every catalogued
    string, the flat link list, and the set of strings NOT yet linked to any rep
    (so the operator can spot pairing gaps). READ-ONLY.
    """
    with get_db() as db:
        reps = db.query(AdRep).order_by(AdRep.display_name, AdRep.email).all()
        strings = (
            db.query(BankMerchantString)
            .order_by(BankMerchantString.normalized_string)
            .all()
        )
        links = (
            db.query(AdRepMerchantLink)
            .order_by(AdRepMerchantLink.id)
            .all()
        )

        # rep_id -> [linked string dicts]
        strings_by_rep: dict[int, list[dict]] = {}
        linked_string_ids: set[int] = set()
        for link in links:
            linked_string_ids.add(link.bank_merchant_string_id)
            s = link.merchant_string
            strings_by_rep.setdefault(link.ad_rep_id, []).append(
                {
                    "link_id": link.id,
                    "bank_merchant_string_id": link.bank_merchant_string_id,
                    "normalized_string": s.normalized_string if s else None,
                    "raw_examples": list(s.raw_examples or []) if s else [],
                    "source": s.source if s else None,
                    "confidence": link.confidence,
                }
            )

        reps_out = []
        for rep in reps:
            item = vendors.rep_to_dict(rep)
            item["strings"] = strings_by_rep.get(rep.id, [])
            reps_out.append(item)

        unlinked = [
            vendors.string_to_dict(s)
            for s in strings
            if s.id not in linked_string_ids
        ]

        return jsonify({
            "reps": reps_out,
            "strings": [vendors.string_to_dict(s) for s in strings],
            "links": [vendors.link_to_dict(link) for link in links],
            "unlinked_strings": unlinked,
            "counts": {
                "reps": len(reps),
                "strings": len(strings),
                "links": len(links),
                "unlinked_strings": len(unlinked),
            },
        })


# --- Reps ------------------------------------------------------------------ #
@confirmed_ctl_bp.route("/vendor-map/reps", methods=["GET"])
def vendor_map_reps_list():
    with get_db() as db:
        reps = db.query(AdRep).order_by(AdRep.display_name, AdRep.email).all()
        return jsonify({"count": len(reps), "reps": [vendors.rep_to_dict(r) for r in reps]})


@confirmed_ctl_bp.route("/vendor-map/reps", methods=["POST"])
def vendor_map_reps_create():
    body = _vendor_body()
    # Accept either a bare email or a full "Name <email>" header (parse both).
    header = (body.get("email") or "").strip()
    display, email, _domain = vendors.parse_email_header(header)
    if not email:
        return jsonify({"error": "email is required"}), 400
    display_name = (body.get("display_name") or display or None)
    with get_db() as db:
        try:
            rep, created = vendors.upsert_ad_rep(
                db,
                email=email,
                display_name=display_name,
                org=(body.get("org") or None),
                notes=(body.get("notes") or None),
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        db.commit()
        return jsonify({"created": created, "rep": vendors.rep_to_dict(rep)}), (
            201 if created else 200
        )


@confirmed_ctl_bp.route("/vendor-map/reps/<int:rep_id>", methods=["PATCH", "PUT"])
def vendor_map_reps_update(rep_id: int):
    body = _vendor_body()
    with get_db() as db:
        rep = db.get(AdRep, rep_id)
        if rep is None:
            return jsonify({"error": "not_found"}), 404
        if "display_name" in body:
            rep.display_name = (body.get("display_name") or None)
        if "org" in body:
            rep.org = (body.get("org") or None)
        if "notes" in body:
            rep.notes = (body.get("notes") or None)
        if "active" in body:
            rep.active = bool(body.get("active"))
        if body.get("email"):
            _d, email, domain = vendors.parse_email_header(body["email"])
            if email:
                rep.email = email
                rep.domain = domain or rep.domain
        db.commit()
        return jsonify({"rep": vendors.rep_to_dict(rep)})


@confirmed_ctl_bp.route("/vendor-map/reps/<int:rep_id>", methods=["DELETE"])
def vendor_map_reps_delete(rep_id: int):
    with get_db() as db:
        rep = db.get(AdRep, rep_id)
        if rep is None:
            return jsonify({"error": "not_found"}), 404
        db.delete(rep)  # cascades to its links
        db.commit()
        return jsonify({"deleted": rep_id})


# --- Merchant strings ------------------------------------------------------ #
@confirmed_ctl_bp.route("/vendor-map/strings", methods=["GET"])
def vendor_map_strings_list():
    with get_db() as db:
        rows = (
            db.query(BankMerchantString)
            .order_by(BankMerchantString.normalized_string)
            .all()
        )
        return jsonify({
            "count": len(rows),
            "strings": [vendors.string_to_dict(s) for s in rows],
        })


@confirmed_ctl_bp.route("/vendor-map/strings", methods=["POST"])
def vendor_map_strings_create():
    body = _vendor_body()
    raw = (body.get("raw_string") or body.get("normalized_string") or "").strip()
    if not raw:
        return jsonify({"error": "raw_string is required"}), 400
    with get_db() as db:
        try:
            row, created = vendors.upsert_merchant_string(
                db, raw_string=raw, source=(body.get("source") or "manual"),
                notes=(body.get("notes") or None),
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        db.commit()
        return jsonify({"created": created, "string": vendors.string_to_dict(row)}), (
            201 if created else 200
        )


@confirmed_ctl_bp.route("/vendor-map/strings/<int:string_id>", methods=["PATCH", "PUT"])
def vendor_map_strings_update(string_id: int):
    body = _vendor_body()
    with get_db() as db:
        row = db.get(BankMerchantString, string_id)
        if row is None:
            return jsonify({"error": "not_found"}), 404
        if "notes" in body:
            row.notes = (body.get("notes") or None)
        if "active" in body:
            row.active = bool(body.get("active"))
        db.commit()
        return jsonify({"string": vendors.string_to_dict(row)})


@confirmed_ctl_bp.route("/vendor-map/strings/<int:string_id>", methods=["DELETE"])
def vendor_map_strings_delete(string_id: int):
    with get_db() as db:
        row = db.get(BankMerchantString, string_id)
        if row is None:
            return jsonify({"error": "not_found"}), 404
        db.delete(row)  # cascades to its links
        db.commit()
        return jsonify({"deleted": string_id})


# --- Links ----------------------------------------------------------------- #
@confirmed_ctl_bp.route("/vendor-map/links", methods=["GET"])
def vendor_map_links_list():
    with get_db() as db:
        links = db.query(AdRepMerchantLink).order_by(AdRepMerchantLink.id).all()
        return jsonify({
            "count": len(links),
            "links": [vendors.link_to_dict(link) for link in links],
        })


@confirmed_ctl_bp.route("/vendor-map/links", methods=["POST"])
def vendor_map_links_create():
    """Create a rep<->string link.

    Accepts existing ids (``ad_rep_id`` / ``bank_merchant_string_id``) and/or
    inline creation values (``email`` and/or ``raw_string``) so the UI can link
    in one call even when a side does not exist yet.
    """
    body = _vendor_body()
    with get_db() as db:
        rep_id = body.get("ad_rep_id")
        string_id = body.get("bank_merchant_string_id")

        if not rep_id and body.get("email"):
            _d, email, _dom = vendors.parse_email_header(body["email"])
            display = (body.get("display_name") or _d or None)
            if email:
                rep, _ = vendors.upsert_ad_rep(
                    db, email=email, display_name=display, org=(body.get("org") or None)
                )
                rep_id = rep.id
        if not string_id and body.get("raw_string"):
            row, _ = vendors.upsert_merchant_string(
                db, raw_string=body["raw_string"], source=(body.get("source") or "manual")
            )
            string_id = row.id

        if not rep_id or not string_id:
            return jsonify({
                "error": "ad_rep_id (or email) and bank_merchant_string_id "
                         "(or raw_string) are required"
            }), 400

        if db.get(AdRep, rep_id) is None:
            return jsonify({"error": "ad_rep not found"}), 404
        if db.get(BankMerchantString, string_id) is None:
            return jsonify({"error": "bank_merchant_string not found"}), 404

        link, created = vendors.link_rep_to_string(
            db,
            ad_rep_id=rep_id,
            bank_merchant_string_id=string_id,
            confidence=(body.get("confidence") or "manual"),
            created_by=(body.get("created_by") or "user"),
            notes=(body.get("notes") or None),
        )
        db.commit()
        return jsonify({"created": created, "link": vendors.link_to_dict(link)}), (
            201 if created else 200
        )


@confirmed_ctl_bp.route("/vendor-map/links/<int:link_id>", methods=["DELETE"])
def vendor_map_links_delete(link_id: int):
    with get_db() as db:
        link = db.get(AdRepMerchantLink, link_id)
        if link is None:
            return jsonify({"error": "not_found"}), 404
        db.delete(link)
        db.commit()
        return jsonify({"deleted": link_id})


@confirmed_ctl_bp.route("/vendor-map/scan", methods=["POST"])
def vendor_map_scan():
    """Seed the merchant-string catalog from local bank_transactions (non-destructive).

    Upserts distinct non-ignored ``bank_transactions.vendor_name`` values as
    ``source='scan'`` catalog rows and reports how many are still unlinked. It
    NEVER auto-creates reps or links (review-first). Optional ``lookback_days``
    bounds the scan by ``txn_date``.
    """
    body = _vendor_body()
    lookback = body.get("lookback_days")
    try:
        lookback = int(lookback) if lookback is not None else None
    except (TypeError, ValueError):
        lookback = None
    with get_db() as db:
        result = vendors.scan_seed_merchant_strings(db, lookback_days=lookback)
        db.commit()
        return jsonify({"status": "ok", **result})


@confirmed_ctl_bp.route("/vendor-map/scan-reps", methods=["POST"])
def vendor_map_scan_reps():
    """Seed ``ad_reps`` from ad-confirmation Gmail From headers (non-destructive).

    Runs the read-only ad-rep Gmail scan (``confirmed_ctl.ingest.rep_scan``):
    harvests EXTERNAL sender addresses from the impersonated mailbox in the
    lookback window and upserts them into ``ad_reps`` (email unique). It NEVER
    creates rep<->string links (``linked_proposed`` is always 0 — links are made
    by a human in the UI) and NEVER touches the CRM.

    Body (all optional): ``lookback_days`` (int, default
    ``AD_REP_SCAN_LOOKBACK_DAYS``) and ``query`` (Gmail query override). A Gmail
    failure surfaces as a controlled 502 rather than an unhandled 500.
    """
    from ..ingest.rep_scan import run_rep_scan

    body = _vendor_body()
    lookback = body.get("lookback_days")
    try:
        lookback = int(lookback) if lookback is not None else None
    except (TypeError, ValueError):
        lookback = None
    query = body.get("query") or None
    with get_db() as db:
        try:
            result = run_rep_scan(db, lookback_days=lookback, query=query)
        except Exception:
            logger.exception("vendor-map scan-reps failed")
            return jsonify({
                "status": "rep_scan_failed",
                "detail": "ad-rep Gmail scan failed; the mailbox is unreachable "
                          "or misconfigured.",
            }), 502
        return jsonify({"status": "ok", **result})
