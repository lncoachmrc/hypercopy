"""Introduce portfolio-based Starter, Plus and Pro pricing.

Revision ID: 0006_pricing_plans
Revises: 0005_position_leverage
"""
from alembic import op

revision = "0006_pricing_plans"
down_revision = "0005_position_leverage"
branch_labels = None
depends_on = None

STARTER_LIMITS = '{"max_multiplier":10,"max_notional_per_trade":2500,"max_positions":100,"max_equity_usd":2500}'
PLUS_LIMITS = '{"max_multiplier":10,"max_notional_per_trade":5000,"max_positions":100,"max_equity_usd":5000}'
PRO_LIMITS = '{"max_multiplier":10,"max_notional_per_trade":10000,"max_positions":100,"included_equity_usd":10000,"excess_fee_annual_pct":5,"wealth_threshold_usd":100000}'
OLD_BASIC_LIMITS = '{"max_multiplier":1,"max_notional_per_trade":2500,"max_positions":5}'
OLD_PRO_LIMITS = '{"max_multiplier":3,"max_notional_per_trade":10000,"max_positions":15}'
OLD_ENTERPRISE_LIMITS = '{"max_multiplier":10,"max_notional_per_trade":1000000,"max_positions":100}'


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        f"""INSERT INTO plans (slug,name,active,limits) VALUES
        ('starter','Starter',true,'{STARTER_LIMITS}'::json),
        ('plus','Plus',true,'{PLUS_LIMITS}'::json),
        ('pro_10k','Pro',true,'{PRO_LIMITS}'::json)
        ON CONFLICT (slug) DO UPDATE SET
          name=EXCLUDED.name, active=EXCLUDED.active, limits=EXCLUDED.limits"""
    )
    # Historical slugs remain valid for already-created Stripe subscriptions.
    bind.exec_driver_sql(f"UPDATE plans SET name='Starter (legacy)', limits='{STARTER_LIMITS}'::json WHERE slug='basic'")
    bind.exec_driver_sql(f"UPDATE plans SET name='Plus (legacy)', limits='{PLUS_LIMITS}'::json WHERE slug='pro'")
    bind.exec_driver_sql(f"UPDATE plans SET name='Pro (legacy)', limits='{PRO_LIMITS}'::json WHERE slug='enterprise'")


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DELETE FROM plans WHERE slug IN ('starter','plus','pro_10k')")
    bind.exec_driver_sql(f"UPDATE plans SET name='Basic', limits='{OLD_BASIC_LIMITS}'::json WHERE slug='basic'")
    bind.exec_driver_sql(f"UPDATE plans SET name='Pro', limits='{OLD_PRO_LIMITS}'::json WHERE slug='pro'")
    bind.exec_driver_sql(f"UPDATE plans SET name='Enterprise', limits='{OLD_ENTERPRISE_LIMITS}'::json WHERE slug='enterprise'")
