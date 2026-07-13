"""ad-rep <-> bank merchant-string registry

Additive-only: creates three NEW tables in the standalone ``confirmed_ctl``
Postgres for the ad-rep <-> bank merchant-string pairing registry:

- ``ad_reps`` — ad-confirmation sender identities (unique lower-cased email).
- ``bank_merchant_strings`` — catalogued BofA merchant/trx strings (unique
  normalized_string; ``raw_examples`` accumulates raw spellings).
- ``ad_rep_merchant_links`` — the many-to-many pairing (unique rep+string,
  ON DELETE CASCADE both sides).

NO existing table is altered and NO CRM/permtrak.com object is touched. Fully
reversible: the downgrade drops the three tables (children first).

Revision ID: 0005_vendor_rep_registry
Revises: 0004_bank_txn_bofa_thread
Create Date: 2026-07-13

"""
import sqlalchemy as sa
from alembic import op

revision = "0005_vendor_rep_registry"
down_revision = "0004_bank_txn_bofa_thread"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ad_reps",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("org", sa.String(length=255), nullable=True),
        sa.Column("domain", sa.String(length=255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("email", name="uq_ad_reps_email"),
    )
    op.create_index("ix_ad_reps_domain", "ad_reps", ["domain"])

    op.create_table(
        "bank_merchant_strings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("normalized_string", sa.String(length=255), nullable=False),
        sa.Column("raw_examples", sa.JSON(), nullable=True),
        sa.Column(
            "source",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'manual'"),
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "normalized_string", name="uq_bank_merchant_strings_normalized"
        ),
        sa.CheckConstraint(
            "source IN ('manual', 'scan', 'bofa_alert')",
            name="ck_bank_merchant_strings_source",
        ),
    )

    op.create_table(
        "ad_rep_merchant_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ad_rep_id", sa.Integer(), nullable=False),
        sa.Column("bank_merchant_string_id", sa.Integer(), nullable=False),
        sa.Column(
            "confidence",
            sa.String(length=10),
            nullable=False,
            server_default=sa.text("'manual'"),
        ),
        sa.Column("created_by", sa.String(length=100), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["ad_rep_id"], ["ad_reps.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["bank_merchant_string_id"],
            ["bank_merchant_strings.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "ad_rep_id",
            "bank_merchant_string_id",
            name="uq_ad_rep_merchant_link",
        ),
    )
    op.create_index(
        "ix_ad_rep_merchant_links_rep", "ad_rep_merchant_links", ["ad_rep_id"]
    )
    op.create_index(
        "ix_ad_rep_merchant_links_string",
        "ad_rep_merchant_links",
        ["bank_merchant_string_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_ad_rep_merchant_links_string", table_name="ad_rep_merchant_links")
    op.drop_index("ix_ad_rep_merchant_links_rep", table_name="ad_rep_merchant_links")
    op.drop_table("ad_rep_merchant_links")
    op.drop_table("bank_merchant_strings")
    op.drop_index("ix_ad_reps_domain", table_name="ad_reps")
    op.drop_table("ad_reps")
