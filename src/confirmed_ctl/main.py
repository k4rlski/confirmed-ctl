"""Click CLI entry point for confirmed-ctl.

Commands:
    process-confirmed   Full pipeline over all confirmed cases
    fetch-receipt       Gmail search + PDF download for one case
    verify-payment      Plaid re-verification for one case
    status              Show confirmed cases + completion state
    watch               Daemon mode — poll on an interval

Read-only by default; CRM writes and Dropbox uploads require --write.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from typing import TypeVar

import click

from . import __version__
from .config import ConfigError, load_config
from .models import CaseOutcome, CaseReport
from .pipeline import Pipeline

T = TypeVar("T")


def _guard(action: Callable[[], T]) -> T:
    """Run a CRM/network-touching action, converting failures to clean errors."""
    try:
        return action()
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001 - surface a friendly message
        raise click.ClickException(
            f"{type(exc).__name__}: {exc}\n"
            "Hint: check CRM credentials in confirmed-ctl.yml "
            "(copy confirmed-ctl.yml.example) or set CONFIRMED_CTL_CRM_* env vars."
        ) from exc


def _configure_logging(level: str, log_file: str = "") -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def _load(ctx: click.Context) -> None:
    try:
        cfg = load_config(ctx.obj.get("config_path"))
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    _configure_logging(cfg.logging.level, cfg.logging.file)
    ctx.obj["config"] = cfg


def _print_report(report: CaseReport) -> None:
    icon = {
        CaseOutcome.DONE: "[DONE]   ",
        CaseOutcome.PARTIAL: "[PARTIAL]",
        CaseOutcome.SKIPPED: "[SKIP]   ",
        CaseOutcome.ERROR: "[ERROR]  ",
    }[report.outcome]
    c = report.case
    click.echo(f"{icon} case {c.case_number} — {c.company} — {c.ad_number} ({c.newspaper_short})")
    for name, value in report.writes.items():
        click.echo(f"           write {name} = {value}")
    for note in report.notes:
        click.echo(f"           note: {note}")


def _summarize(reports: list[CaseReport]) -> None:
    counts: dict[CaseOutcome, int] = {}
    for r in reports:
        counts[r.outcome] = counts.get(r.outcome, 0) + 1
    click.echo("")
    click.echo(
        "Summary: "
        + ", ".join(f"{outcome.value}={counts.get(outcome, 0)}" for outcome in CaseOutcome)
        + f"  (total {len(reports)})"
    )


@click.group()
@click.version_option(__version__, prog_name="confirmed-ctl")
@click.option("--config", "config_path", type=click.Path(), default=None,
              help="Path to confirmed-ctl.yml (default: search CWD + repo root).")
@click.pass_context
def cli(ctx: click.Context, config_path: str | None) -> None:
    """Automated newspaper ad receipt collection + final reconciliation."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


@cli.command("process-confirmed")
@click.option("--dry-run/--write", "dry_run", default=True,
              help="Dry run (default) performs no CRM writes or uploads.")
@click.option("--case", "case_number", default=None, help="Process a single case number.")
@click.option("--hours", "hours", type=int, default=None, help="Bank search window (hours).")
@click.pass_context
def process_confirmed(ctx: click.Context, dry_run: bool, case_number: str | None,
                      hours: int | None) -> None:
    """Run the full pipeline over all confirmed cases."""
    _load(ctx)
    mode = "DRY-RUN" if dry_run else "WRITE"
    click.echo(f"confirmed-ctl process-confirmed [{mode}]")
    pipeline = Pipeline(ctx.obj["config"], dry_run=dry_run)
    reports = _guard(lambda: pipeline.run(case_number=case_number, window_hours=hours))
    for report in reports:
        _print_report(report)
    _summarize(reports)


@cli.command("fetch-receipt")
@click.option("--case", "case_number", required=True, help="Case number to fetch.")
@click.option("--dry-run/--write", "dry_run", default=True)
@click.pass_context
def fetch_receipt(ctx: click.Context, case_number: str, dry_run: bool) -> None:
    """Run only the Gmail search + PDF download step for one case."""
    _load(ctx)
    pipeline = Pipeline(ctx.obj["config"], dry_run=dry_run)
    cases = _guard(lambda: pipeline.crm.fetch_confirmed_cases(case_number=case_number))
    if not cases:
        raise click.ClickException(f"No confirmed case found for {case_number}")
    for case in cases:
        result = pipeline.gmail.fetch(case)
        click.echo(
            f"case {case.case_number}: found={result.found} "
            f"pdf={'yes' if result.has_pdf else 'no'} url={result.thread_url or '-'}"
        )
    pipeline.crm.close()


@cli.command("verify-payment")
@click.option("--case", "case_number", required=True, help="Case number to verify.")
@click.option("--hours", "hours", type=int, default=None, help="Bank search window (hours).")
@click.pass_context
def verify_payment(ctx: click.Context, case_number: str, hours: int | None) -> None:
    """Run only the Plaid transaction re-verification step for one case."""
    _load(ctx)
    pipeline = Pipeline(ctx.obj["config"], dry_run=True)
    cases = _guard(lambda: pipeline.crm.fetch_confirmed_cases(case_number=case_number))
    if not cases:
        raise click.ClickException(f"No confirmed case found for {case_number}")
    for case in cases:
        result = pipeline.plaid.verify(case, window_hours=hours)
        click.echo(
            f"case {case.case_number}: verified={result.verified} "
            f"amount={result.amount} settled={result.settlement_date}"
        )
    pipeline.crm.close()


@cli.command("status")
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show all confirmed cases and their completion state."""
    _load(ctx)
    pipeline = Pipeline(ctx.obj["config"], dry_run=True)
    cases = _guard(pipeline.crm.fetch_confirmed_cases)
    click.echo(f"{len(cases)} confirmed case(s) pending reconciliation:")
    for case in cases:
        click.echo(
            f"  {case.case_number:>8}  {case.newspaper_short:<18} "
            f"{case.ad_number:<14} ${case.invoice_amount:>10.2f}  {case.company}"
        )
    pipeline.crm.close()


@cli.command("watch")
@click.option("--interval", "interval", type=int, default=30,
              help="Polling interval in minutes (default 30).")
@click.option("--dry-run/--write", "dry_run", default=True)
@click.pass_context
def watch(ctx: click.Context, interval: int, dry_run: bool) -> None:
    """Daemon mode — process confirmed cases on an interval."""
    _load(ctx)
    click.echo(f"confirmed-ctl watch — every {interval} min ({'dry-run' if dry_run else 'write'})")
    while True:
        pipeline = Pipeline(ctx.obj["config"], dry_run=dry_run)
        reports = pipeline.run()
        _summarize(reports)
        time.sleep(interval * 60)


if __name__ == "__main__":
    cli()
