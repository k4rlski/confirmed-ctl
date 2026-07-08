"""confirmed_ctl/cli.py

CLI for confirmed-ctl daemon.
Usage:
  confirmed-ctl sync [--lookback-days 2]
  confirmed-ctl status
  confirmed-ctl receipts
  confirmed-ctl match --ad-id 1234
"""
import sys

import click

from . import __version__
from .db.session import get_db
from .gmail.receipts import process_pending_receipts


@click.group()
@click.version_option(__version__, prog_name="confirmed-ctl")
def cli():
    """confirmed-ctl — bank-transaction ingest and ad confirmation tool."""
    pass


@cli.command()
@click.option("--lookback-days", default=2, help="Days to look back when ingesting")
def sync(lookback_days):
    """Ingest recent bank transactions into the local database.

    TODO(phase-later): wire to the BofA email-scan / export ingestion adapters.
    The QuickBooks (QBO) sync backend was removed in Phase 1; the replacement
    adapters arrive in a later generation. This command is currently a stub and
    exits NON-ZERO so cron/automation never treats it as a successful sync.
    """
    click.echo(
        "ERROR: ingestion not implemented yet — no transactions ingested, no "
        "SyncLog written. The QBO sync backend was removed in Phase 1 and the "
        "BofA email-scan / export adapters land in a later gen "
        f"(requested lookback: {lookback_days} days).",
        err=True,
    )
    sys.exit(1)


@cli.command()
def status():
    """Show last sync run info."""
    with get_db() as db:
        from .db.models import SyncLog
        last = db.query(SyncLog).order_by(SyncLog.synced_at.desc()).first()
        if last:
            click.echo(f"Last sync: {last.synced_at}")
            click.echo(f"  Fetched: {last.txns_fetched}, New: {last.txns_new}")
        else:
            click.echo("No sync runs found.")


@cli.command()
def receipts():
    """Download receipts for all confirmed ads that have a Gmail thread ID."""
    click.echo("Processing pending receipts...")
    with get_db() as db:
        result = process_pending_receipts(db)
    click.echo(f"Done: {result['processed']} receipts downloaded.")
    if result["errors"]:
        for e in result["errors"]:
            click.echo(f"  Error: {e}")


@cli.command()
@click.option("--ad-id", required=True, type=int, help="Ad database ID")
def match(ad_id):
    """Show ranked bank transaction candidates for a specific ad."""
    with get_db() as db:
        from .db.models import AdPurchase
        from .matching.scorer import get_candidate_transactions
        ad = db.get(AdPurchase, ad_id)
        if not ad:
            click.echo(f"Ad {ad_id} not found.")
            return
        candidates = get_candidate_transactions(db, ad)
        click.echo(f"\nTop candidates for Ad #{ad.ad_number} ({ad.newspaper_name}, "
                   f"${ad.expected_amount}):\n")
        for i, c in enumerate(candidates, 1):
            t = c["transaction"]
            click.echo(f"  {i}. Score {int(c['score']*100)}% | "
                       f"{t.txn_date} | ${t.total_amount} | {t.vendor_name}")


if __name__ == "__main__":
    cli()
