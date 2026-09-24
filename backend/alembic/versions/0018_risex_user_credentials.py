"""add per-user RISEx trading accounts and signing credentials

Revision ID: 0018_risex_user_credentials
Revises: 0017_master_event_causal_order
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0018_risex_user_credentials"
down_revision = "0017_master_event_causal_order"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "risex_trading_accounts",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_address", sa.String(length=42), nullable=False),
        sa.Column(
            "verified_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_risex_trading_accounts_user_id",
        "risex_trading_accounts",
        ["user_id"],
        unique=True,
    )
    op.create_index(
        "ix_risex_trading_accounts_account_address",
        "risex_trading_accounts",
        ["account_address"],
        unique=True,
    )

    op.create_table(
        "risex_signing_credentials",
        sa.Column(
            "risex_trading_account_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("signer_address", sa.String(length=42), nullable=False),
        sa.Column("ciphertext_b64", sa.Text(), nullable=False),
        sa.Column("nonce_b64", sa.String(length=64), nullable=False),
        sa.Column("wrapped_dek_b64", sa.Text(), nullable=False),
        sa.Column("wrap_nonce_b64", sa.String(length=64), nullable=True),
        sa.Column("key_provider", sa.String(length=24), nullable=False),
        sa.Column("key_reference", sa.String(length=255), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column(
            "generation",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'ACTIVE'"),
            nullable=False,
        ),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
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
        sa.CheckConstraint(
            "generation >= 1",
            name="ck_risex_signing_credentials_generation_positive",
        ),
        sa.ForeignKeyConstraint(
            ["risex_trading_account_id"],
            ["risex_trading_accounts.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_risex_signing_credentials_account_id",
        "risex_signing_credentials",
        ["risex_trading_account_id"],
        unique=True,
    )
    op.create_index(
        "ix_risex_signing_credentials_signer_address",
        "risex_signing_credentials",
        ["signer_address"],
        unique=True,
    )
    op.create_index(
        "ix_risex_signing_credentials_expires_at",
        "risex_signing_credentials",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_risex_signing_credentials_status",
        "risex_signing_credentials",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_risex_signing_credentials_status",
        table_name="risex_signing_credentials",
    )
    op.drop_index(
        "ix_risex_signing_credentials_expires_at",
        table_name="risex_signing_credentials",
    )
    op.drop_index(
        "ix_risex_signing_credentials_signer_address",
        table_name="risex_signing_credentials",
    )
    op.drop_index(
        "ix_risex_signing_credentials_account_id",
        table_name="risex_signing_credentials",
    )
    op.drop_table("risex_signing_credentials")

    op.drop_index(
        "ix_risex_trading_accounts_account_address",
        table_name="risex_trading_accounts",
    )
    op.drop_index(
        "ix_risex_trading_accounts_user_id",
        table_name="risex_trading_accounts",
    )
    op.drop_table("risex_trading_accounts")
