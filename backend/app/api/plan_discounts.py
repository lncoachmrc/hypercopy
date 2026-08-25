from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user, require_csrf, require_role
from app.db.session import get_db
from app.models.discounts import UserPlanDiscount
from app.models.entities import Role, User
from app.services.audit import audit
from app.services.plan_discounts import DISCOUNTABLE_PLANS, discounts_for_user

router = APIRouter(tags=['plan-discounts'])
admin = require_role(Role.ADMIN, Role.SUPERADMIN)


class AdminPlanDiscountIn(BaseModel):
    percent_off: int = Field(ge=0, le=100)
    reason: str = Field(default='Commercial user discount', min_length=3, max_length=500)
    confirmation: str | None = Field(default=None, max_length=80)


@router.get('/subscription/discounts')
async def my_plan_discounts(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return {'discounts': await discounts_for_user(db, user.id)}


@router.get('/admin/plan-discounts')
async def admin_plan_discount_overview(
    limit: int = 50,
    offset: int = 0,
    wallet: str | None = None,
    actor: User = Depends(admin),
    db: AsyncSession = Depends(get_db),
):
    safe_limit = min(max(limit, 1), 100)
    safe_offset = max(offset, 0)
    wallet_query = (wallet or '').strip()

    users_query = select(User)
    total_query = select(func.count(User.id))
    discounted_query = select(func.count(func.distinct(UserPlanDiscount.user_id))).join(
        User, User.id == UserPlanDiscount.user_id
    )
    if wallet_query:
        predicate = User.auth_wallet.ilike(f'%{wallet_query}%')
        users_query = users_query.where(predicate)
        total_query = total_query.where(predicate)
        discounted_query = discounted_query.where(predicate)

    total = int((await db.execute(total_query)).scalar_one())
    discounted_total = int((await db.execute(discounted_query)).scalar_one())
    users = (await db.execute(
        users_query.order_by(User.created_at.desc()).offset(safe_offset).limit(safe_limit)
    )).scalars().all()

    user_ids = [user.id for user in users]
    rows = [] if not user_ids else (await db.execute(
        select(UserPlanDiscount).where(UserPlanDiscount.user_id.in_(user_ids))
    )).scalars().all()
    by_user: dict[uuid.UUID, dict[str, int]] = {}
    for row in rows:
        if row.plan_slug in DISCOUNTABLE_PLANS:
            by_user.setdefault(row.user_id, {})[row.plan_slug] = int(row.percent_off)
    return {
        'users': [
            {
                'id': str(user.id),
                'wallet': user.auth_wallet,
                'role': user.role.value,
                'discounts': by_user.get(user.id, {}),
            }
            for user in users
        ],
        'plans': list(DISCOUNTABLE_PLANS),
        'total': total,
        'discounted_total': discounted_total,
        'offset': safe_offset,
        'limit': safe_limit,
        'wallet_query': wallet_query,
    }


@router.get('/admin/users/{user_id}/plan-discounts')
async def admin_user_plan_discounts(
    user_id: uuid.UUID,
    actor: User = Depends(admin),
    db: AsyncSession = Depends(get_db),
):
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(404, 'User not found')
    return {
        'user_id': str(target.id),
        'wallet': target.auth_wallet,
        'discounts': await discounts_for_user(db, target.id),
    }


@router.post('/admin/users/{user_id}/plan-discounts/{plan_slug}', dependencies=[Depends(require_csrf)])
async def set_admin_user_plan_discount(
    user_id: uuid.UUID,
    plan_slug: str,
    body: AdminPlanDiscountIn,
    actor: User = Depends(admin),
    db: AsyncSession = Depends(get_db),
):
    plan_slug = plan_slug.lower().strip()
    if plan_slug not in DISCOUNTABLE_PLANS:
        raise HTTPException(422, 'Unknown discountable plan')
    if body.percent_off == 100 and body.confirmation != 'APPLY 100% DISCOUNT':
        raise HTTPException(422, '100% discount requires explicit confirmation')

    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(404, 'User not found')

    existing = (await db.execute(select(UserPlanDiscount).where(
        UserPlanDiscount.user_id == target.id,
        UserPlanDiscount.plan_slug == plan_slug,
    ))).scalar_one_or_none()
    previous = int(existing.percent_off) if existing else 0

    if body.percent_off == 0:
        if existing:
            await db.delete(existing)
        action = 'ADMIN_PLAN_DISCOUNT_REMOVED'
    else:
        if existing:
            existing.percent_off = body.percent_off
            existing.updated_by = actor.id
        else:
            db.add(UserPlanDiscount(
                user_id=target.id,
                plan_slug=plan_slug,
                percent_off=body.percent_off,
                updated_by=actor.id,
            ))
        action = 'ADMIN_PLAN_DISCOUNT_SET'

    applies_to = 'direct_activation' if body.percent_off == 100 else 'new_checkout'
    await audit(
        db,
        action=action,
        actor_id=actor.id,
        subject_id=target.id,
        reason=body.reason,
        before={'plan': plan_slug, 'percent_off': previous},
        after={'plan': plan_slug, 'percent_off': body.percent_off, 'applies_to': applies_to},
    )
    await db.commit()
    return {
        'ok': True,
        'user_id': str(target.id),
        'plan': plan_slug,
        'percent_off': body.percent_off,
        'discounts': await discounts_for_user(db, target.id),
        'applies_to_existing_subscription': False,
        'activation_mode': 'direct' if body.percent_off == 100 else 'stripe_checkout',
    }
