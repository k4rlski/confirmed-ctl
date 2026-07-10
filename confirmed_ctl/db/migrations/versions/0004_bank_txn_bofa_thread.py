"""bank_transactions.bofa_gmail_thread_id

Additive-only: adds a single NULLABLE column
``bank_transactions.bofa_gmail_thread_id`` (String 255) that stores the Gmail
thread id of the SOURCE BofA transaction-alert email a row was ingested from.
Captured at ingest from the message stub's ``threadId``; the account-index-
agnostic Gmail deep link is built on read. NULL for rows ingested before this
column existed (backfilled separately via a read-only Gmail lookup CLI/one-off,
never in this schema migration so it stays cleanly reversible).

This is a NEW standalone revision (fang's confirmed_ctl Postgres already applied
0001..0003; only a new revision takes effect there). Fully reversible: the
downgrade drops the new column.

Revision ID: 0004_bank_txn_bofa_thread
Revises: 0003_ignore_memo_patterns
Create Date: 2026-07-10

"""
import sqlalchemy as sa
from alembic import op

revision = "0004_bank_txn_bofa_thread"
down_revision = "0003_ignore_memo_patterns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bank_transactions",
        sa.Column("bofa_gmail_thread_id", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bank_transactions", "bofa_gmail_thread_id")
