"""Deterministic ``source_txn_id`` generation for bank-transaction ingestion.

``bank_transactions`` enforces idempotency via the composite unique constraint
``UNIQUE(source, source_txn_id)`` with BOTH columns ``NOT NULL``. Every
ingestion adapter must therefore always populate ``source_txn_id`` (it is never
``NULL``). This module centralises the convention so all adapters agree:

- **OFX exports (``export-ofx``)** — pass the statement's ``<FITID>`` through as
  ``source_txn_id``. FITID is the bank's own stable per-transaction id.
- **Email-scan (``email-scan``)** — the Gmail message id is a stable, unique
  per-alert id. ``source_txn_id`` is derived DETERMINISTICALLY from it:
  ``<message_id>`` for single-transaction (Type A) alerts, and
  ``<message_id>:<line_index>`` for each line item of a batched (Type B) alert.
  This sidesteps same-day/same-amount natural-key collisions and keeps re-scans
  idempotent (see ``email_scan_source_txn_id``). It is fed through
  ``deterministic_source_txn_id(..., fitid=<derived id>)`` so it is returned
  verbatim.
- **CSV exports (``export-csv``)** — there is no stable per-transaction id, so
  ``source_txn_id`` is a hex SHA-256 hash of the normalized *natural key*:
  ``(source, posted_date ISO, amount, description / merchant, last4)``.

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


def _csv_disambiguator(
    line_index: int | None, balance: float | int | str | Decimal | None
) -> str:
    """Build the export-csv per-row disambiguator suffix (empty when none given).

    A CSV export has no stable per-transaction id, so two genuinely-distinct rows
    on the same day for the same amount + merchant would otherwise hash to the
    same natural key and collapse. This appends whatever distinguishing signal the
    csv ingester can supply — a per-file 0-based ``line_index`` (preferred) and/or
    the running/available ``balance`` on that row — so distinct rows stay distinct
    while re-ingesting the SAME file (same line order) stays idempotent.

    Returns an empty string when neither is provided, so the natural key is
    byte-identical to the historical 5-component key (no churn for callers that
    don't pass a disambiguator).
    """
    parts: list[str] = []
    if line_index is not None:
        parts.append(f"line:{int(line_index)}")
    if balance is not None:
        parts.append(f"bal:{_normalize_amount(balance)}")
    return "|".join(parts)


def deterministic_source_txn_id(
    source: str,
    posted_date: date | datetime | str | None,
    amount: float | int | str | Decimal | None,
    description: str | None,
    last4: str | None = None,
    fitid: str | None = None,
    *,
    line_index: int | None = None,
    balance: float | int | str | Decimal | None = None,
) -> str:
    """Return the ``source_txn_id`` for a transaction (never empty/``None``).

    Convention:

    - If ``fitid`` is provided (OFX path), it is returned verbatim.
    - Otherwise (email-scan / CSV path) a hex SHA-256 of the normalized natural
      key ``(source, posted_date ISO, amount, description/merchant, last4)`` is
      returned.

    For ``export-csv`` ONLY, an optional per-row disambiguator (``line_index``
    and/or ``balance``) is appended to the natural key so two genuinely-distinct
    same-day/same-amount CSV rows do not collapse. Email-scan (message-id, via
    ``fitid``) and OFX (FITID) behavior is unchanged, and when no disambiguator
    is supplied the export-csv hash is identical to the historical key.

    Args:
        source: The ingestion adapter tag (``email-scan`` / ``export-ofx`` /
            ``export-csv``).
        posted_date: Transaction posting date (``date``/``datetime``/ISO str).
        amount: Transaction amount (normalized to a 2-decimal string).
        description: Merchant / description text for the transaction.
        last4: Last four digits of the account/card, if known.
        fitid: The OFX ``<FITID>`` when ingesting from an OFX export.
        line_index: export-csv only — a per-file 0-based line-sequence index that
            disambiguates otherwise-identical rows within the same file.
        balance: export-csv only — the running/available balance on the row, an
            alternative/additional disambiguator when present.

    Returns:
        A non-empty string suitable for ``bank_transactions.source_txn_id``.
    """
    if fitid:
        fitid = str(fitid).strip()
        if fitid:
            return fitid

    components = [
        _normalize_text(source),
        _normalize_date(posted_date),
        _normalize_amount(amount),
        _normalize_text(description),
        _normalize_text(last4),
    ]
    # Per-row disambiguator applies to the export-csv natural-key path only, so
    # email-scan / OFX (both of which return via fitid above anyway) are never
    # affected, and existing export-csv hashes are unchanged when no
    # disambiguator is passed.
    if _normalize_text(source) == "export-csv":
        disambiguator = _csv_disambiguator(line_index, balance)
        if disambiguator:
            components.append(disambiguator)

    natural_key = "|".join(components)
    return hashlib.sha256(natural_key.encode("utf-8")).hexdigest()


def email_scan_source_txn_id(message_id: str, line_index: int | None = None) -> str:
    """Deterministic ``source_txn_id`` for an email-scan transaction.

    The Gmail ``message_id`` is stable and globally unique, so it makes an ideal
    idempotency key that also keeps distinct alerts distinct (avoiding
    same-day/same-amount natural-key collisions):

    - **Type A** (one transaction per alert): ``<message_id>``.
    - **Type B** (batched — multiple line items per alert): pass ``line_index``
      (0-based) to get ``<message_id>:<line_index>`` per line item.

    The result is routed through ``deterministic_source_txn_id`` as ``fitid`` so
    it is returned verbatim and both ``source`` and ``source_txn_id`` stay
    ``NOT NULL``.
    """
    message_id = str(message_id).strip()
    if not message_id:
        raise ValueError("email_scan_source_txn_id requires a non-empty message_id")
    derived = message_id if line_index is None else f"{message_id}:{line_index}"
    return deterministic_source_txn_id(
        "email-scan", None, None, None, fitid=derived
    )
