"""Shared data structures passed between pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


@dataclass
class Case:
    """A single CRM newspaper-ad case eligible for reconciliation.

    Mirrors the columns returned by the trigger query in docs/CRM-SCHEMA.md.
    """

    id: str
    case_number: str
    company: str
    ad_number: str
    invoice_amount: float
    date_invoiced: date | None
    payment_status: str
    news_id: str
    newspaper_name: str
    newspaper_short: str
    trxstring: str | None = None
    gmail_url: str | None = None
    date_paid: date | None = None


@dataclass
class GmailResult:
    """Outcome of the Gmail receipt-collection step."""

    found: bool = False
    thread_url: str | None = None
    pdf_path: str | None = None

    @property
    def has_pdf(self) -> bool:
        return bool(self.pdf_path)


@dataclass
class PlaidResult:
    """Outcome of the Plaid transaction re-verification step."""

    verified: bool = False
    txn_name: str | None = None
    amount: float | None = None
    settlement_date: date | None = None
    trxstring: str | None = None


class CaseOutcome(str, Enum):
    """High-level result classification for a processed case."""

    DONE = "done"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass
class CaseReport:
    """Per-case result summary produced by the pipeline."""

    case: Case
    outcome: CaseOutcome
    gmail: GmailResult = field(default_factory=GmailResult)
    plaid: PlaidResult = field(default_factory=PlaidResult)
    dropbox_path: str | None = None
    dropbox_link: str | None = None
    writes: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        self.notes.append(message)
