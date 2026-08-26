from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.entities import EquitySnapshot, Plan, Subscription, User
from app.services.networking import user_network_state
from app.services.plan_discounts import discount_percent_for

ACTIVE = {'active', 'trialing'}
LEGACY_PLAN_MAP = {'basic': 'starter', 'pro': 'plus', 'enterprise': 'pro_10k'}


async def entitlement(
    db: AsyncSession,
    user: User,
    *,
    portfolio_equity_override: Decimal | None = None,
) -> dict:
    network_state = await user_network_state(db, user.id)
    sub = (await db.execute(select(Subscription).where(Subscription.user_id == user.id))).scalar_one_or_none()
    plan = await db.get(Plan, sub.plan_slug) if sub else None
    now = datetime.now(UTC)
    entitled = False
    if sub and sub.status in ACTIVE:
        deadline = sub.period_end or sub.trial_end
        entitled = deadline is None or deadline > now
    elif sub and sub.status == 'complimentary':
        # Complimentary plans remain entitled only while the administrator keeps
        # a 100% personal discount on that exact plan. Removing or reducing the
        # discount immediately returns the account to the normal paid flow.
        entitled = await discount_percent_for(db, user.id, sub.plan_slug) == 100

    limits = dict(plan.limits if plan else {})
    operator_override = (
        settings.APP_ENV != 'production'
        and network_state.network == 'testnet'
        and user.role.value == 'SUPERADMIN'
    )

    # Staging-only operator override: commercial plan limits must not prevent a
    # SUPERADMIN from validating the complete TESTNET execution pipeline.
    if operator_override:
        limits['max_positions'] = max(int(limits.get('max_positions', 0) or 0), 100)

    if portfolio_equity_override is not None:
        portfolio_equity = Decimal(str(portfolio_equity_override))
    else:
        latest_equity = (await db.execute(
            select(EquitySnapshot)
            .where(
                EquitySnapshot.user_id == user.id,
                EquitySnapshot.taken_at >= network_state.started_at,
            )
            .order_by(EquitySnapshot.taken_at.desc())
            .limit(1)
        )).scalar_one_or_none()
        portfolio_equity = latest_equity.account_value if latest_equity else None

    max_equity = limits.get('max_equity_usd')
    portfolio_limit_exceeded = False
    if max_equity is not None and portfolio_equity is not None and not operator_override:
        portfolio_limit_exceeded = portfolio_equity > Decimal(str(max_equity))
        if portfolio_limit_exceeded:
            entitled = False

    raw_plan = sub.plan_slug if sub else None
    commercial_plan = LEGACY_PLAN_MAP.get(raw_plan, raw_plan)
    return {
        'entitled': entitled,
        'status': sub.status if sub else 'none',
        'plan': raw_plan,
        'commercial_plan': commercial_plan,
        'period_end': sub.period_end.isoformat() if sub and sub.period_end else None,
        'limits': limits,
        'network': network_state.network,
        'portfolio_equity': float(portfolio_equity) if portfolio_equity is not None else None,
        'portfolio_limit_usd': float(max_equity) if max_equity is not None else None,
        'portfolio_limit_exceeded': portfolio_limit_exceeded,
    }
