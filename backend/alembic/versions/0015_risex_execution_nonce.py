"""persist RISEx execution nonce identity

Revision ID: 0015_risex_execution_nonce
Revises: 0014_risex_client_order_id
"""
from alembic import op
import sqlalchemy as sa


revision = "0015_risex_execution_nonce"
down_revision = "0014_risex_client_order_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "executions",
        sa.Column("nonce_anchor", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "executions",
        sa.Column("nonce_bitmap_index", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("executions", "nonce_bitmap_index")
    op.drop_column("executions", "nonce_anchor")
