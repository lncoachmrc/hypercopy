"""Tighten the free trial to the public launch limits.

Revision ID: 0007_trial_limits
Revises: 0006_pricing_plans
"""
from alembic import op

revision = "0007_trial_limits"
down_revision = "0006_pricing_plans"
branch_labels = None
depends_on = None

TRIAL_LIMITS = '{"max_multiplier":1,"max_notional_per_trade":500,"max_positions":3,"max_equity_usd":1000}'
OLD_TRIAL_LIMITS = '{"max_multiplier":1,"max_notional_per_trade":1000,"max_positions":3}'


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        f"""INSERT INTO plans (slug,name,active,limits) VALUES
        ('trial','Trial',true,'{TRIAL_LIMITS}'::json)
        ON CONFLICT (slug) DO UPDATE SET
          name=EXCLUDED.name, active=EXCLUDED.active, limits=EXCLUDED.limits"""
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        f"UPDATE plans SET name='Trial', limits='{OLD_TRIAL_LIMITS}'::json WHERE slug='trial'"
    )
