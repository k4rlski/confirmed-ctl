"""initial confirmed-ctl tables

Creates the tables owned by confirmed-ctl: bank_transactions, ad_confirmations,
and confirmed_ctl_sync_log. The existing CRM table ``ad_purchases`` is assumed to
already exist and is referenced by foreign keys only.

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-08

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bank_transactions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("source_txn_id", sa.String(length=100), nullable=False),
        sa.Column("txn_date", sa.Date(), nullable=False),
        sa.Column("created_time", sa.DateTime(timezone=True)),
        sa.Column("updated_time", sa.DateTime(timezone=True)),
        sa.Column("total_amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("payment_type", sa.String(length=20)),
        sa.Column("payment_ref_num", sa.String(length=100)),
        sa.Column("private_note", sa.Text()),
        sa.Column("doc_number", sa.String(length=100)),
        sa.Column("vendor_id", sa.String(length=50)),
        sa.Column("vendor_name", sa.String(length=255)),
        sa.Column("account_id", sa.String(length=50)),
        sa.Column("account_name", sa.String(length=255)),
        sa.Column("line_descriptions", postgresql.ARRAY(sa.Text())),
        sa.Column("raw_json", postgresql.JSONB()),
        sa.Column(
            "confirmed_ad_id",
            sa.Integer(),
            sa.ForeignKey("ad_purchases.id", ondelete="SET NULL"),
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("created_in_db", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "source", "source_txn_id", name="uq_bank_transactions_source_txn"
        ),
        sa.CheckConstraint(
            "source IN ('email-scan', 'export-ofx', 'export-csv')",
            name="ck_bank_transactions_source",
        ),
    )
    op.create_index("idx_bank_txn_date", "bank_transactions", [sa.text("txn_date DESC")])
    op.create_index("idx_bank_txn_vendor", "bank_transactions", ["vendor_name"])
    op.create_index("idx_bank_txn_amount", "bank_transactions", ["total_amount"])
    op.create_index("idx_bank_txn_confirmed", "bank_transactions", ["confirmed_ad_id"])
    # Partial index for the unmatched-queue candidate query (confirmed_ad_id IS
    # NULL, filtered/sorted by txn_date). See matching/scorer.py.
    op.create_index(
        "idx_bank_txn_unmatched_date",
        "bank_transactions",
        ["txn_date"],
        postgresql_where=sa.text("confirmed_ad_id IS NULL"),
    )

    op.create_table(
        "ad_confirmations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "ad_id", sa.Integer(), sa.ForeignKey("ad_purchases.id"), nullable=False
        ),
        sa.Column("bank_txn_id", sa.Integer(), sa.ForeignKey("bank_transactions.id")),
        sa.Column("gmail_thread_id", sa.String(length=255)),
        sa.Column("gmail_message_id", sa.String(length=255)),
        sa.Column("gmail_subject", sa.Text()),
        sa.Column("receipt_file_path", sa.Text()),
        sa.Column("receipt_url", sa.Text()),
        sa.Column("confirmed_by", sa.String(length=100)),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("match_confidence", sa.String(length=10)),
        sa.Column("match_method", sa.String(length=50)),
        sa.Column("notes", sa.Text()),
        sa.UniqueConstraint("ad_id", name="uq_ad_confirmations_ad_id"),
    )

    op.create_table(
        "confirmed_ctl_sync_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("lookback_days", sa.Integer()),
        sa.Column("txns_fetched", sa.Integer()),
        sa.Column("txns_new", sa.Integer()),
        sa.Column("txns_updated", sa.Integer()),
        sa.Column("auto_matched", sa.Integer()),
        sa.Column("errors", sa.Text()),
        sa.Column("duration_ms", sa.BigInteger()),
    )


def downgrade() -> None:
    op.drop_table("confirmed_ctl_sync_log")
    op.drop_table("ad_confirmations")
    op.drop_index("idx_bank_txn_unmatched_date", table_name="bank_transactions")
    op.drop_index("idx_bank_txn_confirmed", table_name="bank_transactions")
    op.drop_index("idx_bank_txn_amount", table_name="bank_transactions")
    op.drop_index("idx_bank_txn_vendor", table_name="bank_transactions")
    op.drop_index("idx_bank_txn_date", table_name="bank_transactions")
    op.drop_table("bank_transactions")
