"""Deterministic ``source_txn_id`` generation for bank-transaction ingestion.

``bank_transactions`` enforces idempotency via the composite unique constraint
``UNIQUE(source, source_txn_id)`` with BOTH columns ``NOT NULL``. Every
ingestion adapter must therefore always populate ``source_txn_id`` (it is never
``NULL``). This module centralises the convention so all adapters agree:

- **OFX exports (``export-ofx``)** — pass the statement's ``<FITID>`` through as
  ``source_txn_id``. FITID is the bank's own stable per-transaction id.
- **Email-scan (``email-scan``) and CSV exports (``export-csv``)** — there is no
  stable per-transaction id, so ``source_txn_id`` is a hex SHA-256 hash of the
  normalized *natural key*: ``(source, posted_date ISO, amount, description /
  merchant, last4)``.

The natural-key hash is stable across re-ingestion runs of the same underlying
transaction, so re-importing the same statement is a no-op (the unique
constraint collapses duplicates) rather than creating duplicate rows.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal


def _normalize_date(posted_date: date | datetime | str | None) -> str:
    """Return a stable ISO representation of a posting date."""
    if posted_date is None:
        return ""
    if isinstance(posted_date, (date, datetime)):
        return posted_date.isoformat()
    return str(posted_date).strip()


def _normalize_amount(amount: float | int | str | Decimal | None) -> str:
    """Return a stable canonical string for a monetary amount.

    Normalizes to a fixed 2-decimal string so ``12.5`` and ``12.50`` and
    ``Decimal("12.50")`` all hash identically.
    """
    if amount is None:
        return ""
    try:
        return f"{Decimal(str(amount)):.2f}"
    except (ArithmeticError, ValueError):
        return str(amount).strip()


def _normalize_text(value: str | None) -> str:
    """Collapse whitespace and lowercase free-text fields for stable hashing."""
    if not value:
        return ""
    return " ".join(str(value).split()).lower()


def deterministic_source_txn_id(
    source: str,
    posted_date: date | datetime | str | None,
    amount: float | int | str | Decimal | None,
    description: str | None,
    last4: str | None = None,
    fitid: str | None = None,
) -> str:
    """Return the ``source_txn_id`` for a transaction (never empty/``None``).

    Convention:

    - If ``fitid`` is provided (OFX path), it is returned verbatim.
    - Otherwise (email-scan / CSV path) a hex SHA-256 of the normalized natural
      key ``(source, posted_date ISO, amount, description/merchant, last4)`` is
      returned.

    Args:
        source: The ingestion adapter tag (``email-scan`` / ``export-ofx`` /
            ``export-csv``).
        posted_date: Transaction posting date (``date``/``datetime``/ISO str).
        amount: Transaction amount (normalized to a 2-decimal string).
        description: Merchant / description text for the transaction.
        last4: Last four digits of the account/card, if known.
        fitid: The OFX ``<FITID>`` when ingesting from an OFX export.

    Returns:
        A non-empty string suitable for ``bank_transactions.source_txn_id``.
    """
    if fitid:
        fitid = str(fitid).strip()
        if fitid:
            return fitid

    natural_key = "|".join(
        (
            _normalize_text(source),
            _normalize_date(posted_date),
            _normalize_amount(amount),
            _normalize_text(description),
            _normalize_text(last4),
        )
    )
    return hashlib.sha256(natural_key.encode("utf-8")).hexdigest()
