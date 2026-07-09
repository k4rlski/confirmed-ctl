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
    """Ingest recent BofA transaction-alert emails into ``bank_transactions``.

    Runs the read-only Gmail email-scan adapter (both alert missions), inserts
    new transactions idempotently, and records a SyncLog entry. Exits 0 on
    success (printing counts) and non-zero on error.
    """
    from .ingest.email_scan import run_email_scan

    try:
        with get_db() as db:
            result = run_email_scan(db, lookback_days=lookback_days)
    except Exception as exc:
        click.echo(f"ERROR: email-scan sync failed: {exc}", err=True)
        sys.exit(1)

    click.echo(
        "email-scan sync complete "
        f"(lookback {result['lookback_days']}d): "
        f"found={result['found']} inserted={result['inserted']} "
        f"skipped={result['skipped']}"
    )


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


@cli.group()
def ignore():
    """Manage DB-tracked ignore-strings (flag SAAS/vendor charges).

    Bank transactions whose text matches an ACTIVE pattern are flagged
    (``ignored=true``) at ingest so they never surface as reconcile candidates —
    they are flagged, never deleted.
    """
    pass


@ignore.command("add")
@click.argument("pattern")
@click.option("--label", default=None, help="Human-friendly vendor label")
def ignore_add(pattern, label):
    """Add an ignore PATTERN (idempotent). PATTERN is a SHORT stable substring."""
    from .ingest.ignore import add_ignore_pattern

    with get_db() as db:
        row, created = add_ignore_pattern(db, pattern, label)
        db.commit()
        if created:
            click.echo(f"Added ignore pattern #{row.id}: {row.pattern!r} ({row.label})")
        else:
            click.echo(
                f"Ignore pattern already present #{row.id}: {row.pattern!r} "
                f"({row.label}) — no change"
            )


@ignore.command("list")
def ignore_list():
    """List all ignore patterns (id / pattern / label / active)."""
    from .db.models import IgnoreMemoPattern

    with get_db() as db:
        rows = db.query(IgnoreMemoPattern).order_by(IgnoreMemoPattern.id).all()
        if not rows:
            click.echo("No ignore patterns.")
            return
        for r in rows:
            click.echo(
                f"{r.id}\t{r.pattern!r}\t{r.label or ''}\tactive={r.active}"
            )


@ignore.command("seed")
def ignore_seed():
    """Seed the default SAAS/vendor ignore patterns (idempotent)."""
    from .ingest.ignore import seed_default_patterns

    with get_db() as db:
        result = seed_default_patterns(db)
        db.commit()
    click.echo(
        f"Seed complete: inserted={result['inserted']} existing={result['existing']}"
    )


@ignore.command("backfill")
def ignore_backfill():
    """Flag existing bank_transactions matching an active pattern (idempotent)."""
    from .ingest.ignore import backfill_ignored

    with get_db() as db:
        flagged = backfill_ignored(db)
        db.commit()
    click.echo(f"Backfill complete: {flagged} bank_transactions newly flagged ignored.")


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
