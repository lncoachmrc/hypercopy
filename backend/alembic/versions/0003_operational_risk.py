"""Add operational risk state for liquidation-distance gating.

Revision ID: 0003_operational_risk
Revises: 0002_seed
"""
from alembic import op
import sqlalchemy as sa

revision = '0003_operational_risk'
down_revision = '0002_seed'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('risk_state', sa.Column('near_liquidation', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('risk_state', sa.Column('liquidation_distance_pct', sa.Numeric(30, 12), nullable=True))


def downgrade() -> None:
    op.drop_column('risk_state', 'liquidation_distance_pct')
    op.drop_column('risk_state', 'near_liquidation')
