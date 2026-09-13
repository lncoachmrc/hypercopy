"""add RISEx operational execution control requests

Revision ID: 0013_risex_execution_control
Revises: 0012_destination_fence
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0013_risex_execution_control"
down_revision = "0012_destination_fence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "risex_execution_control",
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("control_generation", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("target_worker_id", sa.String(length=80), nullable=False),
        sa.Column("target_boot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by_worker_id", sa.String(length=80), nullable=True),
        sa.Column("consumed_by_boot_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("supersedes_request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("readiness_assertions", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.CheckConstraint("control_generation > 0", name="ck_risex_execution_control_generation_positive"),
        sa.CheckConstraint("action IN ('ARM', 'DISARM')", name="ck_risex_execution_control_action"),
        sa.CheckConstraint("state IN ('REQUESTED', 'CONSUMED')", name="ck_risex_execution_control_state"),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["supersedes_request_id"],
            ["risex_execution_control.request_id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("request_id"),
    )
    op.create_index(
        "ix_risex_execution_control_target_state_generation",
        "risex_execution_control",
        ["target_worker_id", "target_boot_id", "state", "control_generation"],
        unique=False,
    )
    op.create_index(
        "ix_risex_execution_control_requested_at",
        "risex_execution_control",
        ["requested_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_risex_execution_control_requested_at", table_name="risex_execution_control")
    op.drop_index(
        "ix_risex_execution_control_target_state_generation",
        table_name="risex_execution_control",
    )
    op.drop_table("risex_execution_control")
