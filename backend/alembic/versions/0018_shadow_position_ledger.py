"""persist virtual SHADOW follower positions

Revision ID: 0018_shadow_position_ledger
Revises: 0017_master_event_causal_order
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0018_shadow_position_ledger"
down_revision = "0017_master_event_causal_order"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shadow_position_ledger",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_epoch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_provider", sa.String(length=24), nullable=False),
        sa.Column("execution_network", sa.String(length=16), nullable=False),
        sa.Column("shadow_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("asset", sa.String(length=24), nullable=False),
        sa.Column("size", sa.Numeric(30, 12), server_default="0", nullable=False),
        sa.Column("avg_entry_price", sa.Numeric(30, 12), server_default="0", nullable=False),
        sa.Column("residual_entry_notional", sa.Numeric(30, 12), server_default="0", nullable=False),
        sa.Column("mark_price", sa.Numeric(30, 12), server_default="0", nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_simulated_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["execution_epoch_id"],
            ["execution_epochs.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "shadow_started_at",
            "asset",
            name="uq_shadow_position_session_asset",
        ),
    )
    op.create_index(
        "ix_shadow_position_ledger_user_id",
        "shadow_position_ledger",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_shadow_position_ledger_execution_epoch_id",
        "shadow_position_ledger",
        ["execution_epoch_id"],
        unique=False,
    )
    op.create_index(
        "ix_shadow_position_ledger_shadow_started_at",
        "shadow_position_ledger",
        ["shadow_started_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_shadow_position_ledger_shadow_started_at",
        table_name="shadow_position_ledger",
    )
    op.drop_index(
        "ix_shadow_position_ledger_execution_epoch_id",
        table_name="shadow_position_ledger",
    )
    op.drop_index(
        "ix_shadow_position_ledger_user_id",
        table_name="shadow_position_ledger",
    )
    op.drop_table("shadow_position_ledger")
