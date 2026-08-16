"""Seed immutable reference data and safety flags.
Revision ID: 0002_seed
Revises: 0001_initial
"""
from alembic import op

revision='0002_seed'
down_revision='0001_initial'
branch_labels=None
depends_on=None


def upgrade():
    op.execute("""INSERT INTO plans (slug,name,active,limits) VALUES
      ('trial','Trial',true,'{"max_multiplier":1,"max_notional_per_trade":1000,"max_positions":3}'::json),
      ('basic','Basic',true,'{"max_multiplier":1,"max_notional_per_trade":2500,"max_positions":5}'::json),
      ('pro','Pro',true,'{"max_multiplier":3,"max_notional_per_trade":10000,"max_positions":15}'::json),
      ('enterprise','Enterprise',true,'{"max_multiplier":10,"max_notional_per_trade":1000000,"max_positions":100}'::json)
      ON CONFLICT (slug) DO NOTHING""")
    op.execute("""INSERT INTO system_flags (slug,enabled,value,reason) VALUES
      ('live_trading',false,'{}'::json,'Fail-closed default'),
      ('global_pause',false,'{}'::json,'Default'),
      ('emergency_stop',false,'{}'::json,'Default')
      ON CONFLICT (slug) DO NOTHING""")


def downgrade():
    op.execute("DELETE FROM system_flags WHERE slug IN ('live_trading','global_pause','emergency_stop')")
    op.execute("DELETE FROM plans WHERE slug IN ('trial','basic','pro','enterprise')")
