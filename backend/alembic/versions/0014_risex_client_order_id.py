"""add durable RISEx client order id and exposure reservation

Revision ID: 0014_risex_client_order_id
Revises: 0013_risex_execution_control
"""
from alembic import op
import sqlalchemy as sa


revision = "0014_risex_client_order_id"
down_revision = "0013_risex_execution_control"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "executions",
        sa.Column("client_order_id", sa.Numeric(20, 0), nullable=True),
    )
    op.add_column(
        "executions",
        sa.Column("reserved_exposure_usdc", sa.Numeric(30, 12), nullable=True),
    )
    op.create_index(
        "ux_executions_provider_network_client_order_id",
        "executions",
        ["execution_provider", "execution_network", "client_order_id"],
        unique=True,
        postgresql_where=sa.text("client_order_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ux_executions_provider_network_client_order_id",
        table_name="executions",
    )
    op.drop_column("executions", "reserved_exposure_usdc")
    op.drop_column("executions", "client_order_id")
