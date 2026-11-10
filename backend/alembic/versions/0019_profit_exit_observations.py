"""Append-only shadow profit-exit observations (research telemetry).

Revision ID: 0019_profit_exit_observations
Revises: 0018_risex_user_credentials
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0019_profit_exit_observations"
down_revision = "0018_risex_user_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_profit_exit_observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_epoch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_provider", sa.String(24), nullable=False),
        sa.Column("execution_network", sa.String(16), nullable=False),
        sa.Column("asset", sa.String(24), nullable=False),
        sa.Column("source_cycle_id", sa.String(160), nullable=False),
        sa.Column("state_version", sa.BigInteger(), nullable=False),
        sa.Column("evaluation_slot", sa.BigInteger(), nullable=False),
        sa.Column("follower_position_size", sa.Numeric(30, 12)),
        sa.Column("evaluation_basis", sa.String(32), nullable=False),
        sa.Column("pnl_complete", sa.Boolean(), nullable=False),
        sa.Column("funding_model", sa.String(40), nullable=False),
        sa.Column("mark_price", sa.Numeric(30, 12)),
        sa.Column("executable_exit_price", sa.Numeric(30, 12)),
        sa.Column("entry_price", sa.Numeric(30, 12)),
        sa.Column("net_pnl", sa.Numeric(30, 12)),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("position_matches", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(240), nullable=False, server_default=""),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["execution_epoch_id"], ["execution_epochs.id"], ondelete="RESTRICT"),
    )
    op.create_index(
        "ix_profit_exit_observations_scope",
        "ai_profit_exit_observations",
        ["user_id", "execution_epoch_id", "asset", "source_cycle_id", "observed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_profit_exit_observations_scope", table_name="ai_profit_exit_observations")
    op.drop_table("ai_profit_exit_observations")
