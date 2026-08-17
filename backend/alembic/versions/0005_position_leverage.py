"""persist verified master/follower position leverage

Revision ID: 0005_position_leverage
Revises: 0004_ledger_marks
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_position_leverage"
down_revision = "0004_ledger_marks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("position_ledger", sa.Column("master_leverage", sa.Integer(), nullable=True))
    op.add_column("position_ledger", sa.Column("master_is_cross", sa.Boolean(), nullable=True))
    op.add_column("position_ledger", sa.Column("follower_leverage", sa.Integer(), nullable=True))
    op.add_column("position_ledger", sa.Column("follower_is_cross", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("position_ledger", "follower_is_cross")
    op.drop_column("position_ledger", "follower_leverage")
    op.drop_column("position_ledger", "master_is_cross")
    op.drop_column("position_ledger", "master_leverage")
