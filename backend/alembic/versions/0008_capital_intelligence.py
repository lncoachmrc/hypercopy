"""Add capital intelligence and learned master strategy state.

Revision ID: 0008_capital_intelligence
Revises: 0007_trial_limits
"""
from alembic import op

revision = '0008_capital_intelligence'
down_revision = '0007_trial_limits'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("""
        CREATE TABLE master_strategy_profiles (
            id UUID NOT NULL PRIMARY KEY,
            network VARCHAR(16) NOT NULL,
            master_address VARCHAR(42) NOT NULL,
            window_days INTEGER NOT NULL,
            event_count INTEGER NOT NULL,
            asset_count INTEGER NOT NULL,
            profile JSON NOT NULL,
            learned_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT uq_master_strategy_source UNIQUE (network, master_address)
        )
    """)
    bind.exec_driver_sql('CREATE INDEX ix_master_strategy_profiles_learned_at ON master_strategy_profiles (learned_at)')
    bind.exec_driver_sql("""
        CREATE TABLE capital_intelligence_decisions (
            id UUID NOT NULL PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            mode VARCHAR(16) NOT NULL,
            status VARCHAR(32) NOT NULL,
            provider VARCHAR(32),
            model VARCHAR(128),
            candidate_id VARCHAR(48) NOT NULL,
            confidence NUMERIC(30,12),
            follower_equity NUMERIC(30,12) NOT NULL,
            eligible_equity NUMERIC(30,12) NOT NULL,
            recommended_capital NUMERIC(30,12) NOT NULL,
            coverage_pct NUMERIC(30,12) NOT NULL,
            tracking_error_pct NUMERIC(30,12) NOT NULL,
            policy JSON NOT NULL,
            provider_attempts JSON NOT NULL,
            summary TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL
        )
    """)
    bind.exec_driver_sql('CREATE INDEX ix_capital_intelligence_decisions_user_id ON capital_intelligence_decisions (user_id)')
    bind.exec_driver_sql('CREATE INDEX ix_capital_intelligence_decisions_created_at ON capital_intelligence_decisions (created_at)')
    bind.exec_driver_sql('CREATE INDEX ix_capital_intelligence_user_created ON capital_intelligence_decisions (user_id, created_at)')


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql('DROP TABLE IF EXISTS capital_intelligence_decisions')
    bind.exec_driver_sql('DROP TABLE IF EXISTS master_strategy_profiles')
