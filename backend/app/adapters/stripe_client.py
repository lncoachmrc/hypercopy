from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import stripe

from app.core.config import settings

stripe.api_key = settings.STRIPE_SECRET_KEY

PRICE_BY_PLAN = {'basic': settings.STRIPE_PRICE_BASIC, 'pro': settings.STRIPE_PRICE_PRO, 'enterprise': settings.STRIPE_PRICE_ENTERPRISE}


def _period_end(subscription) -> datetime | None:
    value = getattr(subscription, 'current_period_end', None)
    if value:
        return datetime.fromtimestamp(int(value), UTC)
    items = getattr(getattr(subscription, 'items', None), 'data', []) or []
    ends = [getattr(item, 'current_period_end', None) for item in items]
    ends = [int(x) for x in ends if x]
    return datetime.fromtimestamp(max(ends), UTC) if ends else None


async def checkout(*, customer_id: str | None, customer_email: str | None, user_id: str, plan: str) -> str:
    price = PRICE_BY_PLAN.get(plan)
    if not price:
        raise ValueError('Stripe price is not configured for this plan')
    kwargs = dict(
        mode='subscription', line_items=[{'price': price, 'quantity': 1}],
        success_url=f'{settings.PUBLIC_APP_URL}/billing?checkout=success', cancel_url=f'{settings.PUBLIC_APP_URL}/billing',
        client_reference_id=user_id, metadata={'user_id': user_id, 'plan': plan},
        subscription_data={'metadata': {'user_id': user_id, 'plan': plan}},
    )
    if customer_id:
        kwargs['customer'] = customer_id
    elif customer_email:
        kwargs['customer_email'] = customer_email
    session = await asyncio.to_thread(stripe.checkout.Session.create, **kwargs)
    return session.url


async def portal(customer_id: str) -> str:
    session = await asyncio.to_thread(stripe.billing_portal.Session.create, customer=customer_id, return_url=f'{settings.PUBLIC_APP_URL}/billing')
    return session.url


def construct_event(payload: bytes, signature: str):
    return stripe.Webhook.construct_event(payload, signature, settings.STRIPE_WEBHOOK_SECRET)


async def get_subscription(subscription_id: str):
    return await asyncio.to_thread(stripe.Subscription.retrieve, subscription_id, expand=['items.data.price'])
