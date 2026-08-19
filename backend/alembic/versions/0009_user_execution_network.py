"""add per-user Hyperliquid execution network and epoch

Revision ID: 0009_user_execution_network
Revises: 0008_equity_breakdown
"""
from alembic import op
import sqlalchemy as sa

revision = "0009_user_execution_network"
down_revision = "0008_equity_breakdown"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("execution_network", sa.String(length=16), nullable=False, server_default="testnet"),
    )
    op.add_column(
        "users",
        sa.Column("network_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Existing TRAXION follower history is TESTNET in the current rollout. Use
    # each user's creation time as the first network epoch so the migration does
    # not make existing equity/PnL history disappear from the dashboard.
    op.execute("UPDATE users SET network_started_at = created_at WHERE network_started_at IS NULL")
    op.alter_column(
        "users",
        "network_started_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )
    op.create_index("ix_users_execution_network", "users", ["execution_network"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_users_execution_network", table_name="users")
    op.drop_column("users", "network_started_at")
    op.drop_column("users", "execution_network")
