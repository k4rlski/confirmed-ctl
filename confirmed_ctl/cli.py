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
@click.option("--ad-crm-id", required=True, help="CRM ad id (EspoCRM record id)")
def match(ad_crm_id):
    """Show ranked bank transaction candidates for a specific CRM ad.

    TODO(phase-later): hydrate the ad from the read-only MariaDB CRM
    (``permtrak2_crm.t_e_s_t_p_e_r_m``; see docs/CRM-SCHEMA.md) into a
    ``CrmAd`` and pass it to ``get_candidate_transactions``. Ad data is never
    stored in confirmed-ctl Postgres, so there is nothing to look up until that
    read adapter lands; the scoring path is otherwise ready.
    """
    click.echo(
        "ERROR: CRM ad lookup is not implemented yet — the read-only "
        "permtrak2_crm.t_e_s_t_p_e_r_m adapter lands in a later gen. Ad data is "
        f"never stored in confirmed-ctl Postgres (requested ad_crm_id: {ad_crm_id}).",
        err=True,
    )
    sys.exit(1)


if __name__ == "__main__":
    cli()
