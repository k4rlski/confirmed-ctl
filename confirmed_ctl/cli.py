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
    """Download receipts for all confirmed ads that have a Gmail thread ID.

    Back-compat alias for ``receipt download-receipts`` (strict keyword mode).
    """
    click.echo("Processing pending receipts...")
    with get_db() as db:
        result = process_pending_receipts(db)
    click.echo(
        f"Done: {result['processed']} ads updated, "
        f"{result['downloaded']} files downloaded, {result['skipped']} skipped."
    )
    if result["errors"]:
        for e in result["errors"]:
            click.echo(f"  Error: {e}")


@cli.group()
def receipt():
    """Receipt-CTL (confirmed-ctl / ad-buy half): pull receipt PDFs from the
    ad-confirmation Gmail thread of CONFIRMED ads and record them on
    ``ad_confirmations``. Read-only against Gmail; the BofA alert thread is never
    touched. The vendor-portal scraper half lives in the standalone receipt-ctl.
    """
    pass


def _print_receipt_result(result, *, dry_run):
    verb = "would download" if dry_run else "downloaded"
    # dry-run never writes files (downloaded/saved stay 0), so report the
    # aggregate would_download count; a real run reports files actually written.
    count = result["would_download"] if dry_run else result["downloaded"]
    click.echo(
        f"pending={result['pending']} ads_updated={result['processed']} "
        f"{verb}={count} skipped={result['skipped']}"
    )
    for d in result["details"]:
        tag = d.get("ad_number") or d.get("ad_crm_id")
        # dry-run lists filenames in would_download; a real run lists saved paths
        # (plus any already-present files recovered via dedup).
        accepted = d.get("saved") or d.get("would_download") or []
        if accepted:
            click.echo(f"  [{tag}] {verb}: " + ", ".join(accepted))
        for p in d.get("present", []):
            click.echo(f"  [{tag}] already on disk: {p}")
        for s in d["skipped"]:
            click.echo(f"  [{tag}] skip {s['filename']!r} ({s['reason']})")
    for e in result["errors"]:
        click.echo(f"  Error: {e}", err=True)


@receipt.command("gmail-scan")
@click.option("--ad-crm-id", default=None, help="Limit to one CRM ad id")
@click.option("--loose", is_flag=True, help="Accept any non-denylisted PDF (no keyword)")
def receipt_gmail_scan(ad_crm_id, loose):
    """DRY-RUN: classify attachments on pending ad-confirmation threads.

    Downloads nothing and writes nothing — reports which attachments WOULD be
    accepted as receipts vs skipped (and why). Use before a real download.
    """
    with get_db() as db:
        result = process_pending_receipts(
            db, ad_crm_id=ad_crm_id, require_keyword=not loose, dry_run=True
        )
    _print_receipt_result(result, dry_run=True)


@receipt.command("download-receipts")
@click.option("--ad-crm-id", default=None, help="Limit to one CRM ad id")
@click.option("--loose", is_flag=True, help="Accept any non-denylisted PDF (no keyword)")
@click.option("--dry-run", is_flag=True, help="Classify only; do not write files/DB")
def receipt_download(ad_crm_id, loose, dry_run):
    """Download accepted receipt PDFs and record ``receipt_file_path``.

    Scope: confirmed ads with a Gmail thread and no receipt yet. SHA-256
    de-duplicated on disk. Pass ``--ad-crm-id`` to process a single ad first.
    """
    with get_db() as db:
        result = process_pending_receipts(
            db, ad_crm_id=ad_crm_id, require_keyword=not loose, dry_run=dry_run
        )
    _print_receipt_result(result, dry_run=dry_run)


@receipt.command("xfer-receipts")
def receipt_xfer():
    """Transfer stored receipts to the Dropbox case tree (NOT YET IMPLEMENTED).

    The Dropbox transfer is deferred (non-goal this cycle); receipts currently
    live under ``RECEIPTS_BASE_PATH`` on fang and are referenced from Postgres.
    """
    click.echo(
        "xfer-receipts is not implemented yet — Dropbox transfer is deferred. "
        "Receipts live under RECEIPTS_BASE_PATH on fang (Postgres holds the path)."
    )


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


@cli.group()
def vendors():
    """Ad-rep <-> bank merchant-string registry (fang Postgres only).

    Seed/inspect the ``ad_reps`` / ``bank_merchant_strings`` /
    ``ad_rep_merchant_links`` tables. Never touches the CRM.
    """
    pass


@vendors.command("scan")
@click.option(
    "--lookback-days",
    default=None,
    type=int,
    help="Bound the scan to bank txns within N days (default: all).",
)
def vendors_scan(lookback_days):
    """Seed merchant strings from bank_transactions (non-destructive upsert)."""
    from .vendors import scan_seed_merchant_strings

    with get_db() as db:
        result = scan_seed_merchant_strings(db, lookback_days=lookback_days)
        db.commit()
    click.echo(
        "vendors scan complete: "
        f"scanned={result['scanned']} created={result['created']} "
        f"existing={result['existing']} unlinked={result['unlinked_count']}"
    )


@vendors.command("list")
def vendors_list():
    """List reps, merchant strings, and links (id / key)."""
    from .db.models import AdRep, AdRepMerchantLink, BankMerchantString

    with get_db() as db:
        reps = db.query(AdRep).order_by(AdRep.id).all()
        strings = db.query(BankMerchantString).order_by(BankMerchantString.id).all()
        links = db.query(AdRepMerchantLink).order_by(AdRepMerchantLink.id).all()
        click.echo(f"# Reps ({len(reps)})")
        for r in reps:
            click.echo(f"  {r.id}\t{r.email}\t{r.display_name or ''}\t{r.org or ''}")
        click.echo(f"# Merchant strings ({len(strings)})")
        for s in strings:
            click.echo(f"  {s.id}\t{s.normalized_string}\t[{s.source}]")
        click.echo(f"# Links ({len(links)})")
        for lnk in links:
            click.echo(
                f"  {lnk.id}\trep={lnk.ad_rep_id}\tstring={lnk.bank_merchant_string_id}"
                f"\t{lnk.confidence}"
            )


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
