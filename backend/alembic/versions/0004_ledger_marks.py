"""persist per-asset marks and exchange free margin

Revision ID: 0004_ledger_marks
Revises: 0003_operational_risk
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_ledger_marks"
down_revision = "0003_operational_risk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("position_ledger", sa.Column("mark_price", sa.Numeric(30, 12), nullable=False, server_default="0"))
    op.add_column("equity_snapshots", sa.Column("free_margin", sa.Numeric(30, 12), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("equity_snapshots", "free_margin")
    op.drop_column("position_ledger", "mark_price")
