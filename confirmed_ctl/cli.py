"""confirmed_ctl/cli.py

CLI for confirmed-ctl daemon.
Usage:
  confirmed-ctl sync [--lookback-days 2]
  confirmed-ctl status
  confirmed-ctl receipts
  confirmed-ctl match --ad-id 1234
"""
import click

from . import __version__
from .db.session import get_db
from .gmail.receipts import process_pending_receipts
from .qbo.sync import sync_recent_transactions


@click.group()
@click.version_option(__version__, prog_name="confirmed-ctl")
def cli():
    """confirmed-ctl — QBO bank sync and ad confirmation tool."""
    pass


@cli.command()
@click.option("--lookback-days", default=2, help="Days to look back in QBO")
@click.option("--no-cdc", is_flag=True, help="Use date query instead of CDC")
def sync(lookback_days, no_cdc):
    """Sync recent QBO transactions to local database."""
    click.echo(f"Syncing last {lookback_days} days from QuickBooks...")
    with get_db() as db:
        summary = sync_recent_transactions(db, lookback_days=lookback_days, use_cdc=not no_cdc)
    click.echo(f"Done: {summary['new']} new, {summary['updated']} updated, "
               f"{summary['fetched']} total fetched.")
    if summary["errors"]:
        click.echo("Errors:")
        for e in summary["errors"]:
            click.echo(f"  {e}")


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
