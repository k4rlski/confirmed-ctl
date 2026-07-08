"""add source column to confirmed_ctl_sync_log

The ``SyncLog.source`` column records which ingestion adapter produced a sync
run (e.g. ``email-scan``). It is added as a standalone migration (rather than an
in-place edit to ``0001_initial``) because fang's confirmed_ctl Postgres already
applied ``0001_initial``; only a new revision will take effect there.

Revision ID: 0002_add_synclog_source
Revises: 0001_initial
Create Date: 2026-07-08

"""
import sqlalchemy as sa
from alembic import op

revision = "0002_add_synclog_source"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "confirmed_ctl_sync_log",
        sa.Column("source", sa.String(length=50), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("confirmed_ctl_sync_log", "source")
