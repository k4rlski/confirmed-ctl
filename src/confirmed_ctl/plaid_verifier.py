"""plaid-ctl integration — re-verify a bank transaction for a confirmed case.

plaid-ctl already matched the charge and set the case to ``PaymentConfirmed``.
confirmed-ctl re-verifies the transaction still exists, the amount matches within
tolerance, and captures the settlement date for the final CRM write.

The subprocess bridge to plaid-ctl is intentionally abstracted behind
``PlaidVerifier.verify`` so it can be swapped for a direct import or API call
later. ``format_trxstring`` is a pure function and is unit-tested.
"""

from __future__ import annotations

import logging
from datetime import date

from .config import PlaidConfig
from .models import Case, PlaidResult

logger = logging.getLogger(__name__)


def format_trxstring(settlement_date: date, txn_name: str, amount: float) -> str:
    """Build the CRM ``trxstring`` value: ``"{date} | {txn_name} | ${amount}"``.

    Example: ``2026-03-10 | MIAMI HERALD MEDIA CO | $1368.00``
    """
    return f"{settlement_date.strftime('%Y-%m-%d')} | {txn_name} | ${amount:.2f}"


def amount_matches(invoice_amount: float, txn_amount: float, tolerance: float) -> bool:
    """True when the transaction amount matches the invoice within tolerance."""
    return abs(invoice_amount - txn_amount) <= tolerance


class PlaidVerifier:
    """Re-verify a case's bank transaction via plaid-ctl."""

    def __init__(self, config: PlaidConfig, dry_run: bool = True):
        self.config = config
        self.dry_run = dry_run

    def verify(self, case: Case, window_hours: int | None = None) -> PlaidResult:
        """Re-verify the transaction for ``case``.

        NOTE: the live plaid-ctl bridge is not yet implemented (Phase 1). This
        returns an unverified result so the pipeline degrades gracefully; the
        subprocess/API call will be wired in during implementation.
        """
        window = window_hours or self.config.default_window_hours
        logger.debug(
            "verify case=%s ad=%s amount=%.2f window=%sh",
            case.case_number,
            case.ad_number,
            case.invoice_amount,
            window,
        )
        # TODO(phase1): shell out to plaid-ctl / call its API, parse the matched
        # transaction, and populate txn_name / amount / settlement_date.
        return PlaidResult(verified=False)
