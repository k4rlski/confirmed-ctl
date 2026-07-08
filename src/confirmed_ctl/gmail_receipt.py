"""gmail-ctl integration — search for the ad-confirmation email + PDF receipt.

For each confirmed case, search the configured Gmail accounts for the ad number
(e.g. ``IPR00160880``), capture the thread URL, and download the PDF attachment
if present. Some newspapers confirm by email body only (no attachment); that is a
valid partial outcome handled by the pipeline.
"""

from __future__ import annotations

import logging

from .config import GmailConfig
from .models import Case, GmailResult

logger = logging.getLogger(__name__)


class GmailReceiptFetcher:
    """Search Gmail (via gmail-ctl) and download receipt PDFs."""

    def __init__(self, config: GmailConfig, staging_dir: str, dry_run: bool = True):
        self.config = config
        self.staging_dir = staging_dir
        self.dry_run = dry_run

    def fetch(self, case: Case) -> GmailResult:
        """Search for the case's confirmation email and download its PDF.

        NOTE: the live gmail-ctl bridge is not yet implemented (Phase 1). Returns
        a not-found result so the pipeline degrades gracefully.
        """
        logger.debug(
            "gmail search case=%s ad=%s accounts=%s",
            case.case_number,
            case.ad_number,
            ",".join(self.config.accounts),
        )
        # TODO(phase1): shell out to gmail-ctl to search each account for
        # case.ad_number, capture the thread URL, and download the PDF attachment
        # into self.staging_dir.
        return GmailResult(found=False)

    def thread_url_only(self, url: str) -> GmailResult:
        """Helper for the 'found email but no PDF attachment' partial case."""
        return GmailResult(found=True, thread_url=url, pdf_path=None)

    def _staged_pdf_path(self, case: Case) -> str | None:
        return None
