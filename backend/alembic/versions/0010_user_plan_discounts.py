"""add per-user plan discounts

Revision ID: 0010_user_plan_discounts
Revises: 0009_user_execution_network
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0010_user_plan_discounts"
down_revision = "0009_user_execution_network"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_plan_discounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_slug", sa.String(length=32), nullable=False),
        sa.Column("percent_off", sa.Integer(), nullable=False),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("percent_off >= 1 AND percent_off <= 100", name="ck_user_plan_discount_percent"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "plan_slug", name="uq_user_plan_discount"),
    )
    op.create_index("ix_user_plan_discounts_user_id", "user_plan_discounts", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_user_plan_discounts_user_id", table_name="user_plan_discounts")
    op.drop_table("user_plan_discounts")
