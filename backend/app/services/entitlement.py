from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.entities import Plan, Subscription, User

ACTIVE = {'active', 'trialing'}


async def entitlement(db: AsyncSession, user: User) -> dict:
    sub = (await db.execute(select(Subscription).where(Subscription.user_id == user.id))).scalar_one_or_none()
    plan = None
    if sub:
        plan = await db.get(Plan, sub.plan_slug)
    now = datetime.now(UTC)
    entitled = False
    if sub and sub.status in ACTIVE:
        deadline = sub.period_end or sub.trial_end
        entitled = deadline is None or deadline > now

    limits = dict(plan.limits if plan else {})
    # Staging-only operator override: commercial plan limits must not prevent
    # a SUPERADMIN from validating the full multi-asset execution pipeline on
    # Hyperliquid TESTNET. Production/customer entitlements are unchanged.
    if (
        settings.APP_ENV != 'production'
        and settings.follower_network == 'testnet'
        and user.role.value == 'SUPERADMIN'
    ):
        limits['max_positions'] = max(int(limits.get('max_positions', 0) or 0), 100)

    return {
        'entitled': entitled,
        'status': sub.status if sub else 'none',
        'plan': sub.plan_slug if sub else None,
        'period_end': sub.period_end.isoformat() if sub and sub.period_end else None,
        'limits': limits,
    }
