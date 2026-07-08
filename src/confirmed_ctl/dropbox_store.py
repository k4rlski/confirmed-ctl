"""Dropbox receipt storage via rclone.

Path convention (see docs/DROPBOX.md):

    Receipts/Newspapers/{Year}/{YYYY-MM}/{NewspaperShortName}/
      Case-{casenumber}_{CompanySlug}_{AdNumber}_{DateInvoiced}.pdf

The path-building helpers are pure functions so they can be unit-tested without
rclone or network access. The actual upload is a thin subprocess wrapper.
"""

from __future__ import annotations

import logging
import re
import subprocess
from datetime import date

from .config import DropboxConfig
from .models import Case

logger = logging.getLogger(__name__)

_SLUG_CLEAN = re.compile(r"[^A-Za-z0-9]+")


def company_slug(company: str, max_len: int = 30) -> str:
    """Company name → filesystem-safe slug: non-alphanumerics to hyphens.

    Spaces and punctuation collapse to single hyphens, trimmed to ``max_len``.
    """
    slug = _SLUG_CLEAN.sub("-", company.strip()).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug


def receipt_filename(case: Case) -> str:
    """Build the receipt PDF filename for a case."""
    invoiced = case.date_invoiced.strftime("%Y-%m-%d") if case.date_invoiced else "unknown-date"
    return (
        f"Case-{case.case_number}_{company_slug(case.company)}"
        f"_{case.ad_number}_{invoiced}.pdf"
    )


def receipt_dir(case: Case, base_path: str) -> str:
    """Build the Dropbox directory (without remote prefix) for a case."""
    invoiced = case.date_invoiced or date.today()
    year = invoiced.strftime("%Y")
    year_month = invoiced.strftime("%Y-%m")
    return f"{base_path}/{year}/{year_month}/{case.newspaper_short}"


def remote_path(case: Case, config: DropboxConfig) -> str:
    """Full rclone remote path including the remote name prefix."""
    directory = receipt_dir(case, config.base_path)
    return f"{config.remote}:{directory}/{receipt_filename(case)}"


class DropboxStore:
    """Thin rclone wrapper for uploading receipts and generating share links."""

    def __init__(self, config: DropboxConfig, dry_run: bool = True):
        self.config = config
        self.dry_run = dry_run

    def upload(self, local_path: str, case: Case) -> str:
        """Upload ``local_path`` to the computed remote path. Returns remote path."""
        target = remote_path(case, self.config)
        if self.dry_run:
            logger.info("[dry-run] would upload %s -> %s", local_path, target)
            return target
        directory = f"{self.config.remote}:{receipt_dir(case, self.config.base_path)}"
        result = subprocess.run(
            ["rclone", "copyto", local_path, target],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"rclone upload failed for {target}: {result.stderr.strip()}")
        logger.info("uploaded %s -> %s", local_path, directory)
        return target

    def shared_link(self, target: str) -> str:
        """Generate a Dropbox shared link for an already-uploaded remote path."""
        if self.dry_run:
            logger.info("[dry-run] would generate shared link for %s", target)
            return ""
        result = subprocess.run(
            ["rclone", "link", target],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning("rclone link failed for %s: %s", target, result.stderr.strip())
            return ""
        return result.stdout.strip()
