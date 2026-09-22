"""persist durable causal order on master events

Revision ID: 0017_master_event_causal_order
Revises: 0016_ai_profit_exit_decisions
"""
from alembic import op
import sqlalchemy as sa


revision = "0017_master_event_causal_order"
down_revision = "0016_ai_profit_exit_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Historical rows intentionally remain NULL:
    # their causal ordering predates this feature and must not be inferred.
    op.add_column(
        "master_events",
        sa.Column("causal_order", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_master_events_causal_order",
        "master_events",
        ["causal_order"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_master_events_causal_order",
        table_name="master_events",
    )
    op.drop_column("master_events", "causal_order")
