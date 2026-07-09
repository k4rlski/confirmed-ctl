"""Smoke tests: the core package imports without heavy optional deps installed.

Google, chromadb, flask, and psycopg2 are intentionally NOT required to import
the CLI or the scoring/sync modules — they are loaded lazily at call time.
"""

import importlib


def test_core_modules_import():
    for mod in [
        "confirmed_ctl",
        "confirmed_ctl.settings",
        "confirmed_ctl.cli",
        "confirmed_ctl.daemon",
        "confirmed_ctl.db.models",
        "confirmed_ctl.db.session",
        "confirmed_ctl.gmail.client",
        "confirmed_ctl.gmail.receipts",
        "confirmed_ctl.matching.scorer",
        "confirmed_ctl.matching.rag",
        "confirmed_ctl.crm",
        "confirmed_ctl.crm.client",
        "confirmed_ctl.ingest.ignore",
    ]:
        importlib.import_module(mod)


def test_cli_help_and_version():
    from click.testing import CliRunner

    from confirmed_ctl.cli import cli

    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "0.2.0" in result.output

    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    for cmd in ("sync", "status", "receipts", "match", "ignore"):
        assert cmd in result.output

    # The ignore group exposes add/list/seed/backfill.
    result = runner.invoke(cli, ["ignore", "--help"])
    assert result.exit_code == 0
    for cmd in ("add", "list", "seed", "backfill"):
        assert cmd in result.output


def test_allowed_write_semantics_models_present():
    from confirmed_ctl.db.models import AdConfirmation, BankTransaction, SyncLog

    assert BankTransaction.__tablename__ == "bank_transactions"
    assert AdConfirmation.__tablename__ == "ad_confirmations"
    assert SyncLog.__tablename__ == "confirmed_ctl_sync_log"
