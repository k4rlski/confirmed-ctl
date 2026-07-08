"""Per-case orchestration: Gmail → Plaid verify → Dropbox → CRM write.

Implements the partial-completion decision table from docs/DESIGN.md. The
pipeline never writes to the CRM unless ``dry_run`` is False; even then, only
allow-listed fields are written by ``CrmClient``.
"""

from __future__ import annotations

import logging

from .config import Config
from .crm_client import CrmClient
from .dropbox_store import DropboxStore
from .gmail_receipt import GmailReceiptFetcher
from .models import Case, CaseOutcome, CaseReport
from .plaid_verifier import PlaidVerifier, amount_matches, format_trxstring

logger = logging.getLogger(__name__)


class Pipeline:
    """Coordinates all stages for a batch of cases."""

    def __init__(self, config: Config, dry_run: bool = True):
        self.config = config
        self.dry_run = dry_run
        self.crm = CrmClient(config.crm, dry_run=dry_run)
        self.gmail = GmailReceiptFetcher(
            config.gmail, staging_dir=config.dropbox.staging_dir, dry_run=dry_run
        )
        self.plaid = PlaidVerifier(config.plaid, dry_run=dry_run)
        self.dropbox = DropboxStore(config.dropbox, dry_run=dry_run)

    def process_case(self, case: Case, window_hours: int | None = None) -> CaseReport:
        """Run the full pipeline for a single case and classify the outcome."""
        report = CaseReport(case=case, outcome=CaseOutcome.SKIPPED)

        gmail = self.gmail.fetch(case)
        report.gmail = gmail
        plaid = self.plaid.verify(case, window_hours=window_hours)
        report.plaid = plaid

        # Store the receipt PDF in Dropbox when we have one.
        if gmail.has_pdf:
            try:
                target = self.dropbox.upload(gmail.pdf_path, case)
                report.dropbox_path = target
                report.dropbox_link = self.dropbox.shared_link(target)
            except Exception as exc:  # noqa: BLE001 - report + continue
                report.outcome = CaseOutcome.ERROR
                report.note(f"Dropbox upload failed: {exc}")
                logger.error("case %s dropbox upload failed: %s", case.case_number, exc)
                return report
        elif gmail.found:
            report.dropbox_path = None
            report.note("Gmail found but no PDF attachment")

        # Validate the Plaid amount against the invoice.
        plaid_ok = plaid.verified and plaid.amount is not None and amount_matches(
            case.invoice_amount, plaid.amount, self.config.plaid.amount_tolerance
        )
        if plaid.verified and not plaid_ok:
            report.note(
                f"Plaid amount ${plaid.amount} != invoice ${case.invoice_amount} "
                "(outside tolerance) — manual review"
            )

        writes = self._compute_writes(case, report, gmail_found=gmail.found, plaid_ok=plaid_ok)
        report.writes = writes

        if writes:
            self.crm.write_case_fields(case, writes)

        report.outcome = self._classify(gmail_found=gmail.found, plaid_ok=plaid_ok, writes=writes)
        return report

    def _compute_writes(
        self, case: Case, report: CaseReport, *, gmail_found: bool, plaid_ok: bool
    ) -> dict[str, str]:
        """Determine allow-listed CRM writes per the partial-completion table."""
        writes: dict[str, str] = {}

        if gmail_found and report.gmail.thread_url:
            writes["urlgmailadconfirm"] = report.gmail.thread_url

        if plaid_ok:
            plaid = report.plaid
            trxstring = plaid.trxstring or format_trxstring(
                plaid.settlement_date, plaid.txn_name or "", plaid.amount or 0.0
            )
            writes["trxstring"] = trxstring
            settle = CrmClient.format_date(plaid.settlement_date)
            if settle:
                writes["datepaidnews"] = settle

        # Only mark Done when both sides are satisfied.
        if gmail_found and plaid_ok:
            writes["statacctgcreditnews"] = self.config.crm.done_status

        return writes

    @staticmethod
    def _classify(*, gmail_found: bool, plaid_ok: bool, writes: dict[str, str]) -> CaseOutcome:
        if gmail_found and plaid_ok:
            return CaseOutcome.DONE
        if writes:
            return CaseOutcome.PARTIAL
        return CaseOutcome.SKIPPED

    def run(
        self, case_number: str | None = None, window_hours: int | None = None
    ) -> list[CaseReport]:
        """Fetch confirmed cases and process each one."""
        cases = self.crm.fetch_confirmed_cases(case_number=case_number)
        logger.info("processing %d confirmed case(s)", len(cases))
        reports = [self.process_case(c, window_hours=window_hours) for c in cases]
        self.crm.close()
        return reports
