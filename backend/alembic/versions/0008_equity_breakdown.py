"""persist the Hyperliquid equity breakdown and snapshot account mode

Revision ID: 0008_equity_breakdown
Revises: 0007_trial_limits
"""
from alembic import op
import sqlalchemy as sa

revision = "0008_equity_breakdown"
down_revision = "0007_trial_limits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "equity_snapshots",
        sa.Column("collateral_balance", sa.Numeric(30, 12), nullable=False, server_default="0"),
    )
    op.add_column(
        "equity_snapshots",
        sa.Column("unrealized_pnl", sa.Numeric(30, 12), nullable=False, server_default="0"),
    )
    op.add_column(
        "equity_snapshots",
        sa.Column("account_mode", sa.String(length=32), nullable=False, server_default="unknown"),
    )


def downgrade() -> None:
    op.drop_column("equity_snapshots", "account_mode")
    op.drop_column("equity_snapshots", "unrealized_pnl")
    op.drop_column("equity_snapshots", "collateral_balance")
