"""persist AI profit-exit decisions and same-cycle execution memory

Revision ID: 0016_ai_profit_exit_decisions
Revises: 0015_risex_execution_nonce
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0016_ai_profit_exit_decisions"
down_revision = "0015_risex_execution_nonce"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_profit_exit_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "execution_epoch_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("execution_provider", sa.String(length=24), nullable=False),
        sa.Column("execution_network", sa.String(length=16), nullable=False),
        sa.Column("asset", sa.String(length=24), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("source_cycle_id", sa.String(length=160), nullable=False),
        sa.Column(
            "source_cycle_open_event_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "source_master_position",
            sa.Numeric(30, 12),
            nullable=False,
        ),
        sa.Column(
            "follower_position_size",
            sa.Numeric(30, 12),
            nullable=False,
        ),
        sa.Column("state_version", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("intent_state", sa.String(length=24), nullable=True),
        sa.Column("net_pnl", sa.Numeric(30, 12), nullable=True),
        sa.Column(
            "pnl_complete",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "position_verified_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "decision_inputs",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
        sa.Column("decision_reason", sa.Text(), nullable=False),
        sa.Column("model_provider", sa.String(length=32), nullable=False),
        sa.Column("model_name", sa.String(length=80), nullable=False),
        sa.Column("model_version", sa.String(length=80), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("copy_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('HOLD', 'CLOSE_PROFIT', 'ABSTAIN')",
            name="ck_ai_profit_exit_action",
        ),
        sa.CheckConstraint(
            "intent_state IS NULL OR intent_state IN "
            "('PENDING', 'PARTIAL', 'COMPLETED', 'FAILED', 'AMBIGUOUS')",
            name="ck_ai_profit_exit_intent_state",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["execution_epoch_id"],
            ["execution_epochs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_cycle_open_event_id"],
            ["master_events.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["copy_job_id"],
            ["copy_jobs.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(
        "ix_ai_profit_exit_decisions_user_id",
        "ai_profit_exit_decisions",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_ai_profit_exit_decisions_execution_epoch_id",
        "ai_profit_exit_decisions",
        ["execution_epoch_id"],
        unique=False,
    )
    op.create_index(
        "ix_ai_profit_exit_decisions_source_cycle_open_event_id",
        "ai_profit_exit_decisions",
        ["source_cycle_open_event_id"],
        unique=False,
    )
    op.create_index(
        "ix_ai_profit_exit_decisions_copy_job_id",
        "ai_profit_exit_decisions",
        ["copy_job_id"],
        unique=False,
    )
    op.create_index(
        "ix_ai_profit_exit_decisions_expires_at",
        "ai_profit_exit_decisions",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_ai_profit_exit_scope_cycle",
        "ai_profit_exit_decisions",
        [
            "user_id",
            "execution_provider",
            "execution_network",
            "asset",
            "source_cycle_id",
        ],
        unique=False,
    )
    op.create_index(
        "ix_ai_profit_exit_state_expiry",
        "ai_profit_exit_decisions",
        ["intent_state", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ai_profit_exit_state_expiry",
        table_name="ai_profit_exit_decisions",
    )
    op.drop_index(
        "ix_ai_profit_exit_scope_cycle",
        table_name="ai_profit_exit_decisions",
    )
    op.drop_index(
        "ix_ai_profit_exit_decisions_expires_at",
        table_name="ai_profit_exit_decisions",
    )
    op.drop_index(
        "ix_ai_profit_exit_decisions_copy_job_id",
        table_name="ai_profit_exit_decisions",
    )
    op.drop_index(
        "ix_ai_profit_exit_decisions_source_cycle_open_event_id",
        table_name="ai_profit_exit_decisions",
    )
    op.drop_index(
        "ix_ai_profit_exit_decisions_execution_epoch_id",
        table_name="ai_profit_exit_decisions",
    )
    op.drop_index(
        "ix_ai_profit_exit_decisions_user_id",
        table_name="ai_profit_exit_decisions",
    )
    op.drop_table("ai_profit_exit_decisions")
