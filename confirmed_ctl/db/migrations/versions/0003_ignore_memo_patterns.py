"""ignore_memo_patterns table + bank_transactions ignore flags

Adds DB-tracked ignore-strings so recurring SAAS/vendor charges are excluded
from reconcile candidates (flagged, never deleted). Additive-only:

- new table ``ignore_memo_patterns`` (short stable substring + label + active),
- ``bank_transactions.ignored`` (bool NOT NULL default false),
- ``bank_transactions.ignore_reason`` (text, nullable).

This is a standalone revision (rather than editing an applied migration) because
fang's confirmed_ctl Postgres already applied 0001/0002; only a new revision
takes effect there. Seeding of the default patterns and the one-time backfill of
existing rows are done via the ``confirmed-ctl ignore seed`` / ``ignore backfill``
CLI commands (kept out of the schema migration so it stays cleanly reversible).

Revision ID: 0003_ignore_memo_patterns
Revises: 0002_add_synclog_source
Create Date: 2026-07-09

"""
import sqlalchemy as sa
from alembic import op

revision = "0003_ignore_memo_patterns"
down_revision = "0002_add_synclog_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ignore_memo_patterns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pattern", sa.Text(), nullable=False),
        sa.Column("label", sa.Text()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )
    # Case-insensitive lookups on ``pattern``.
    op.create_index(
        "ix_ignore_memo_patterns_pattern_ci",
        "ignore_memo_patterns",
        [sa.text("lower(pattern)")],
    )

    op.add_column(
        "bank_transactions",
        sa.Column(
            "ignored", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "bank_transactions",
        sa.Column("ignore_reason", sa.Text()),
    )


def downgrade() -> None:
    op.drop_column("bank_transactions", "ignore_reason")
    op.drop_column("bank_transactions", "ignored")
    op.drop_index(
        "ix_ignore_memo_patterns_pattern_ci", table_name="ignore_memo_patterns"
    )
    op.drop_table("ignore_memo_patterns")
