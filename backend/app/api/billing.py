from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import stripe_client
from app.api.deps import current_user, require_csrf
from app.core.config import settings
from app.db.session import get_db
from app.models.entities import Plan, StripeEvent, Subscription, User
from app.services.audit import audit
from app.services.entitlement import entitlement

router = APIRouter(tags=['billing'])


@router.get('/subscription')
async def subscription(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await entitlement(db, user)


@router.post('/subscription/checkout', dependencies=[Depends(require_csrf)])
async def create_checkout(body: dict, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    plan = str(body.get('plan', '')).lower()
    if plan not in {'basic','pro','enterprise'}:
        raise HTTPException(422, 'Unknown plan')
    sub = (await db.execute(select(Subscription).where(Subscription.user_id == user.id))).scalar_one_or_none()
    if sub and sub.stripe_subscription_id and sub.status in {'active','trialing','past_due'}:
        raise HTTPException(409, 'An existing Stripe subscription must be managed through the billing portal')
    url = await stripe_client.checkout(customer_id=sub.stripe_customer_id if sub else None, customer_email=user.email, user_id=str(user.id), plan=plan)
    return {'url': url}


@router.post('/subscription/portal', dependencies=[Depends(require_csrf)])
async def create_portal(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    sub = (await db.execute(select(Subscription).where(Subscription.user_id == user.id))).scalar_one_or_none()
    if not sub or not sub.stripe_customer_id:
        raise HTTPException(409, 'No Stripe customer exists yet')
    return {'url': await stripe_client.portal(sub.stripe_customer_id)}


@router.post('/webhooks/stripe')
async def stripe_webhook(request: Request, stripe_signature: str = Header(alias='Stripe-Signature'), db: AsyncSession = Depends(get_db)):
    payload = await request.body()
    try:
        event = stripe_client.construct_event(payload, stripe_signature)
    except Exception as exc:
        raise HTTPException(400, 'Invalid Stripe webhook signature') from exc
    event_id = str(event['id'])
    inserted = (await db.execute(
        insert(StripeEvent).values(
            event_id=event_id, type=str(event['type']),
            payload_hash=hashlib.sha256(payload).hexdigest(),
        ).on_conflict_do_nothing(index_elements=[StripeEvent.event_id]).returning(StripeEvent.id)
    )).scalar_one_or_none()
    if inserted is None:
        await db.rollback()
        return {'received': True, 'duplicate': True}
    obj = event['data']['object']
    etype = str(event['type'])

    if etype == 'checkout.session.completed':
        uid = obj.get('client_reference_id') or obj.get('metadata', {}).get('user_id')
        if uid:
            sub = (await db.execute(select(Subscription).where(Subscription.user_id == uuid.UUID(str(uid))))).scalar_one_or_none()
            if sub:
                sub.stripe_customer_id = str(obj.get('customer')) if obj.get('customer') else sub.stripe_customer_id
                sub.stripe_subscription_id = str(obj.get('subscription')) if obj.get('subscription') else sub.stripe_subscription_id
                # Never grant entitlement here; verified subscription webhook is authoritative.
    elif etype.startswith('customer.subscription.'):
        uid = obj.get('metadata', {}).get('user_id')
        if uid:
            sub = (await db.execute(select(Subscription).where(Subscription.user_id == uuid.UUID(str(uid))))).scalar_one_or_none()
            if sub:
                sub.stripe_customer_id = str(obj.get('customer')) if obj.get('customer') else sub.stripe_customer_id
                sub.stripe_subscription_id = str(obj.get('id'))
                sub.status = 'canceled' if etype.endswith('.deleted') else str(obj.get('status', sub.status))
                plan = obj.get('metadata', {}).get('plan')
                if plan and await db.get(Plan, plan): sub.plan_slug = plan
                end = obj.get('current_period_end')
                if not end:
                    ends = [i.get('current_period_end') for i in obj.get('items', {}).get('data', []) if i.get('current_period_end')]
                    end = max(ends) if ends else None
                if end: sub.period_end = datetime.fromtimestamp(int(end), UTC)
    elif etype == 'invoice.payment_failed':
        customer = str(obj.get('customer') or '')
        sub = (await db.execute(select(Subscription).where(Subscription.stripe_customer_id == customer))).scalar_one_or_none()
        if sub: sub.status = 'past_due'
    await db.commit()
    return {'received': True}
