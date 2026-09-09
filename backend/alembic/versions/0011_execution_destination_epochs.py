"""add execution provider and destination epochs

Revision ID: 0011_execution_destination_epochs
Revises: 0010_user_plan_discounts
"""
from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0011_execution_destination_epochs"
down_revision = "0010_user_plan_discounts"
branch_labels = None
depends_on = None


_PROVIDER_CHECK = "execution_provider IN ('hyperliquid', 'risex')"
_NETWORK_CHECK = "network IN ('mainnet', 'testnet')"


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "execution_provider",
            sa.String(length=24),
            nullable=False,
            server_default="hyperliquid",
        ),
    )
    op.create_check_constraint(
        "ck_users_execution_provider",
        "users",
        _PROVIDER_CHECK,
    )
    op.create_index(
        "ix_users_execution_provider",
        "users",
        ["execution_provider"],
        unique=False,
    )

    op.create_table(
        "execution_epochs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=24), nullable=False),
        sa.Column("network", sa.String(length=16), nullable=False),
        sa.Column("account_address", sa.String(length=128), nullable=True),
        sa.Column("credential_version", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "provider IN ('hyperliquid', 'risex')",
            name="ck_execution_epochs_provider",
        ),
        sa.CheckConstraint(_NETWORK_CHECK, name="ck_execution_epochs_network"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_execution_epochs_user_id",
        "execution_epochs",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_execution_epochs_provider_network",
        "execution_epochs",
        ["provider", "network"],
        unique=False,
    )
    op.create_index(
        "uq_execution_epochs_one_active_per_user",
        "execution_epochs",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )

    op.add_column(
        "users",
        sa.Column(
            "active_execution_epoch_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_users_active_execution_epoch",
        "users",
        "execution_epochs",
        ["active_execution_epoch_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_users_active_execution_epoch_id",
        "users",
        ["active_execution_epoch_id"],
        unique=False,
    )

    bind = op.get_bind()
    legacy_rows = bind.execute(
        sa.text(
            """
            SELECT
                u.id AS user_id,
                u.execution_network AS network,
                u.network_started_at AS started_at,
                ta.account_address AS account_address,
                sc.key_version AS credential_version
            FROM users u
            LEFT JOIN trading_accounts ta
              ON ta.user_id = u.id
            LEFT JOIN signing_credentials sc
              ON sc.trading_account_id = ta.id
            """
        )
    ).mappings().all()

    insert_epoch = sa.text(
        """
        INSERT INTO execution_epochs (
            id, user_id, provider, network, account_address,
            credential_version, started_at, ended_at
        ) VALUES (
            :id, :user_id, 'hyperliquid', :network, :account_address,
            :credential_version, :started_at, NULL
        )
        """
    )
    set_active_epoch = sa.text(
        """
        UPDATE users
        SET execution_provider = 'hyperliquid',
            active_execution_epoch_id = :epoch_id
        WHERE id = :user_id
        """
    )

    for row in legacy_rows:
        epoch_id = uuid.uuid4()
        bind.execute(
            insert_epoch,
            {
                "id": epoch_id,
                "user_id": row["user_id"],
                "network": row["network"],
                "account_address": row["account_address"],
                "credential_version": row["credential_version"],
                "started_at": row["started_at"],
            },
        )
        bind.execute(
            set_active_epoch,
            {"epoch_id": epoch_id, "user_id": row["user_id"]},
        )


def downgrade() -> None:
    op.drop_index("ix_users_active_execution_epoch_id", table_name="users")
    op.drop_constraint("fk_users_active_execution_epoch", "users", type_="foreignkey")
    op.drop_column("users", "active_execution_epoch_id")

    op.drop_index("uq_execution_epochs_one_active_per_user", table_name="execution_epochs")
    op.drop_index("ix_execution_epochs_provider_network", table_name="execution_epochs")
    op.drop_index("ix_execution_epochs_user_id", table_name="execution_epochs")
    op.drop_table("execution_epochs")

    op.drop_index("ix_users_execution_provider", table_name="users")
    op.drop_constraint("ck_users_execution_provider", "users", type_="check")
    op.drop_column("users", "execution_provider")
