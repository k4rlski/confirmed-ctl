"""confirmed_ctl/daemon.py

Daemon loop for confirmed-ctl. Wakes on an interval, runs a QBO sync, sleeps.
Interval configured via SYNC_INTERVAL_SECONDS (default: 3600 = 1 hour).
"""

from __future__ import annotations

import logging
import time

from . import settings
from .db.session import get_db
from .qbo.sync import sync_recent_transactions

log = logging.getLogger("confirmed-ctl.daemon")


def run():
    log.info("confirmed-ctl daemon starting. Interval: %ss.", settings.SYNC_INTERVAL_SECONDS)
    while True:
        try:
            with get_db() as db:
                summary = sync_recent_transactions(db, lookback_days=2)
            log.info(
                "Sync complete: %s new, %s updated, %s fetched.",
                summary["new"], summary["updated"], summary["fetched"],
            )
        except Exception as e:
            log.error("Sync error: %s", e)
        time.sleep(settings.SYNC_INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
