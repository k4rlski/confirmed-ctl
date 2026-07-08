from datetime import date

import pytest

from confirmed_ctl.models import Case


@pytest.fixture
def sample_case() -> Case:
    """Case 10349 — Eduexplora / Miami Herald (from docs/CRM-SCHEMA.md)."""
    return Case(
        id="abc123",
        case_number="10349",
        company="Eduexplora International",
        ad_number="IPR00160880",
        invoice_amount=1368.00,
        date_invoiced=date(2026, 3, 2),
        payment_status="PaymentConfirmed",
        news_id="n001",
        newspaper_name="Miami Herald",
        newspaper_short="Miami-Herald",
    )
