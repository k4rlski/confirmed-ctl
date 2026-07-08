"""confirmed_ctl/daemon.py

Daemon loop for confirmed-ctl. Wakes on an interval, runs an ingestion pass,
sleeps. Interval configured via SYNC_INTERVAL_SECONDS (default: 3600 = 1 hour).

TODO(phase-later): wire the loop to the BofA email-scan / export ingestion
adapters. The QuickBooks (QBO) sync backend was removed in Phase 1; until the
replacement adapters land in a later generation the loop only heartbeats.
"""

from __future__ import annotations

import logging
import time

from . import settings

log = logging.getLogger("confirmed-ctl.daemon")


def run():
    log.info(
        "confirmed-ctl daemon starting. Interval: %ss. Ingestion adapters are "
        "not wired yet (QBO sync removed in Phase 1); the loop is idle until "
        "the BofA email-scan / export adapters land.",
        settings.SYNC_INTERVAL_SECONDS,
    )
    while True:
        # TODO(phase-later): call the ingestion adapter(s) here.
        log.info(
            "confirmed-ctl daemon heartbeat: idle — no ingestion adapter wired, "
            "no transactions ingested this cycle."
        )
        time.sleep(settings.SYNC_INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
