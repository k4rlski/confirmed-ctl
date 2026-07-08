"""confirmed_ctl/matching/scorer.py

Given an unconfirmed ad record, return ranked bank transaction candidates.
Scoring uses: vendor name similarity, amount match, date proximity.
"""
from __future__ import annotations

from datetime import date, timedelta
from difflib import SequenceMatcher

from sqlalchemy.orm import Session

from ..db.models import BankTransaction, CrmAd

# Configurable weights
WEIGHT_AMOUNT = 0.50   # Exact or near-exact amount is strongest signal
WEIGHT_VENDOR = 0.30   # Vendor name substring match
WEIGHT_DATE = 0.20     # Date proximity

# Credit-card service fee: newspapers billed to a card post the invoice amount
# grossed up by a 3.99% processing fee. A bank txn near expected_amount OR near
# expected_amount * this multiplier is an equally strong amount match.
CC_FEE_MULTIPLIER = 1.0399

# Strong-match tolerance around a target amount: within $1 OR within 0.5%.
AMOUNT_TOLERANCE_ABS = 1.00
AMOUNT_TOLERANCE_PCT = 0.005

# Known abbreviation map — extend as real BofA vendor strings are observed.
KNOWN_MAPPINGS = {
    "los angeles times": ["la times", "latimes", "l.a. times"],
    "miami herald": ["herald", "miami herald"],
    "sun sentinel": ["sentinel", "sun-sentinel"],
    "chicago tribune": ["tribune", "chi tribune"],
    "new york times": ["nyt", "ny times"],
    "houston chronicle": ["chronicle", "houston chron"],
}


def get_candidate_transactions(
    db: Session,
    ad: CrmAd,
    lookback_days: int = 5,
    top_n: int = 8,
) -> list[dict]:
    """
    Return top_n ranked bank transactions as candidates for confirming this ad.

    ``ad`` is a :class:`~confirmed_ctl.db.models.CrmAd` read view of the MariaDB
    CRM record; it must have: expected_amount, newspaper_name, expected_charge_date
    (or run_date). Candidates are the *unmatched* bank transactions
    (``confirmed_ad_crm_id IS NULL``) inside the date window.
    """
    charge_date = ad.expected_charge_date or ad.run_date
    if charge_date is None:
        # Without a date anchor there is no window to search — return no
        # candidates rather than raising, so the popup/CLI degrade gracefully.
        return []
    window_start = charge_date - timedelta(days=lookback_days)
    window_end = charge_date + timedelta(days=2)  # charges can post slightly late

    # Pull candidate transactions — pre-filter by date window and unmatched only.
    candidates = (
        db.query(BankTransaction)
        .filter(
            BankTransaction.txn_date >= window_start,
            BankTransaction.txn_date <= window_end,
            BankTransaction.confirmed_ad_crm_id.is_(None),
        )
        .all()
    )

    scored = []
    for txn in candidates:
        score = _score_candidate(txn, ad)
        if score > 0.10:  # minimum threshold — filters obviously irrelevant txns
            scored.append({"transaction": txn, "score": score})

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_n]


def _score_candidate(txn: BankTransaction, ad: CrmAd) -> float:
    # expected_amount can be NULL in the CRM (pricenewsreal). Skip the amount
    # signal entirely (contribute 0) rather than raising on float(None) — the
    # candidate can still score on vendor + date proximity.
    if ad.expected_amount is None:
        amount_score = 0.0
    else:
        amount_score = _score_amount_ccfee(float(txn.total_amount), float(ad.expected_amount))
    vendor_score = _score_vendor(txn.vendor_name, ad.newspaper_name)
    date_score = _score_date(txn.txn_date, ad.expected_charge_date or ad.run_date)

    return (
        WEIGHT_AMOUNT * amount_score
        + WEIGHT_VENDOR * vendor_score
        + WEIGHT_DATE * date_score
    )


def _score_amount(actual: float, expected: float) -> float:
    if expected == 0:
        return 0.0
    diff_pct = abs(actual - expected) / expected
    if diff_pct == 0:
        return 1.0
    elif diff_pct <= 0.01:   # within 1%
        return 0.90
    elif diff_pct <= 0.05:   # within 5%
        return 0.60
    elif diff_pct <= 0.15:   # within 15%
        return 0.30
    return 0.0


def _score_amount_ccfee(actual: float, expected: float | None) -> float:
    """CC-fee-aware amount score.

    A bank charge posts either the invoice amount or that amount grossed up by
    the 3.99% credit-card service fee. The txn amount is matched (by magnitude,
    so debits stored as negatives still match) against BOTH ``expected`` and
    ``expected * CC_FEE_MULTIPLIER``; the best target wins. A target hit within
    $1 or 0.5% counts as a strong match. Anything else falls back to the plain
    percentage buckets in :func:`_score_amount` against the raw ``expected``.

    ``expected`` may be ``None`` (CRM ``pricenewsreal`` NULL); that yields no
    amount contribution rather than raising.
    """
    if expected is None or expected == 0:
        return 0.0
    magnitude = abs(actual)
    best = 0.0
    for target in (expected, expected * CC_FEE_MULTIPLIER):
        diff = abs(magnitude - target)
        if diff == 0:
            return 1.0
        if diff <= AMOUNT_TOLERANCE_ABS or diff / abs(target) <= AMOUNT_TOLERANCE_PCT:
            best = max(best, 0.95)
    # Fall back to the plain graduated buckets against the un-feed expected.
    return max(best, _score_amount(magnitude, expected))


def _score_vendor(txn_vendor: str | None, ad_newspaper: str | None) -> float:
    if not txn_vendor or not ad_newspaper:
        return 0.0
    v1 = txn_vendor.lower().strip()
    v2 = ad_newspaper.lower().strip()

    # Direct substring: "LA TIMES" in "LOS ANGELES TIMES ACH"
    if v2 in v1 or v1 in v2:
        return 1.0

    for canonical, aliases in KNOWN_MAPPINGS.items():
        if canonical in v2 or v2 in canonical:
            for alias in aliases:
                if alias in v1:
                    return 0.90

    # Fuzzy fallback
    ratio = SequenceMatcher(None, v1, v2).ratio()
    return ratio if ratio > 0.5 else 0.0


def _score_date(txn_date: date | None, expected_date: date | None) -> float:
    if not txn_date or not expected_date:
        return 0.0
    diff = abs((txn_date - expected_date).days)
    if diff == 0:
        return 1.0
    if diff == 1:
        return 0.85
    if diff == 2:
        return 0.65
    if diff == 3:
        return 0.40
    if diff <= 5:
        return 0.20
    return 0.0
