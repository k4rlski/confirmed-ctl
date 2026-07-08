"""confirmed_ctl/daemon.py

Daemon loop for confirmed-ctl. Wakes on an interval, runs the BofA email-scan
ingestion pass, sleeps. Interval configured via SYNC_INTERVAL_SECONDS
(default: 3600 = 1 hour); lookback via EMAIL_SCAN_LOOKBACK_DAYS.
"""

from __future__ import annotations

import logging
import time

from . import settings

log = logging.getLogger("confirmed-ctl.daemon")


def _run_once() -> None:
    """Run a single email-scan pass. Errors are logged, never fatal to the loop."""
    from .db.session import get_db
    from .ingest.email_scan import run_email_scan

    try:
        with get_db() as db:
            result = run_email_scan(db, lookback_days=settings.EMAIL_SCAN_LOOKBACK_DAYS)
        log.info(
            "email-scan cycle: found=%s inserted=%s skipped=%s (lookback %sd)",
            result["found"],
            result["inserted"],
            result["skipped"],
            result["lookback_days"],
        )
    except Exception:
        log.exception("email-scan cycle failed; will retry next interval")


def run():
    log.info(
        "confirmed-ctl daemon starting. Interval: %ss, lookback: %sd. Running the "
        "BofA email-scan ingestion adapter each cycle.",
        settings.SYNC_INTERVAL_SECONDS,
        settings.EMAIL_SCAN_LOOKBACK_DAYS,
    )
    while True:
        _run_once()
        time.sleep(settings.SYNC_INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
