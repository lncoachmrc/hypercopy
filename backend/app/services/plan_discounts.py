from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.discounts import UserPlanDiscount

DISCOUNTABLE_PLANS = ('starter', 'plus', 'pro_10k')
PLAN_LABELS = {'starter': 'Starter', 'plus': 'Plus', 'pro_10k': 'Pro'}


async def discounts_for_user(db: AsyncSession, user_id: uuid.UUID) -> dict[str, int]:
    rows = (await db.execute(
        select(UserPlanDiscount).where(UserPlanDiscount.user_id == user_id)
    )).scalars().all()
    return {row.plan_slug: int(row.percent_off) for row in rows if row.plan_slug in DISCOUNTABLE_PLANS}


async def discount_percent_for(db: AsyncSession, user_id: uuid.UUID, plan_slug: str) -> int:
    value = (await db.execute(
        select(UserPlanDiscount.percent_off).where(
            UserPlanDiscount.user_id == user_id,
            UserPlanDiscount.plan_slug == plan_slug,
        )
    )).scalar_one_or_none()
    return int(value or 0)


def apply_percent_discount(amount: float, percent_off: int) -> float:
    pct = max(0, min(int(percent_off), 100))
    return round(float(amount) * (100 - pct) / 100, 2)
