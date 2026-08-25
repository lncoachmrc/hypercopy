from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import stripe

from app.core.config import settings

stripe.api_key = settings.STRIPE_SECRET_KEY


def price_for(plan: str, billing_period: str) -> str:
    period = billing_period.lower()
    if period not in {'monthly', 'yearly'}:
        return ''
    mapping = {
        ('starter', 'monthly'): settings.STRIPE_PRICE_STARTER_MONTHLY or settings.STRIPE_PRICE_BASIC,
        ('starter', 'yearly'): settings.STRIPE_PRICE_STARTER_YEARLY,
        ('plus', 'monthly'): settings.STRIPE_PRICE_PLUS_MONTHLY or settings.STRIPE_PRICE_PRO,
        ('plus', 'yearly'): settings.STRIPE_PRICE_PLUS_YEARLY,
        ('pro_10k', 'monthly'): settings.STRIPE_PRICE_PRO_MONTHLY or settings.STRIPE_PRICE_ENTERPRISE,
        ('pro_10k', 'yearly'): settings.STRIPE_PRICE_PRO_YEARLY,
    }
    return mapping.get((plan, period), '')


def plan_configured(plan: str, billing_period: str) -> bool:
    return bool(price_for(plan, billing_period))


def personal_coupon_id(plan: str, percent_off: int) -> str:
    safe_plan = ''.join(ch for ch in plan.lower() if ch.isalnum() or ch in {'-', '_'})
    pct = max(1, min(int(percent_off), 100))
    return f'traxion-{safe_plan}-{pct}-pct'


def _is_missing_coupon_error(exc: Exception) -> bool:
    return getattr(exc, 'http_status', None) == 404 or 'No such coupon' in str(exc)


def _ensure_personal_coupon(plan: str, percent_off: int) -> str:
    coupon_id = personal_coupon_id(plan, percent_off)
    try:
        coupon = stripe.Coupon.retrieve(coupon_id)
        if getattr(coupon, 'valid', True):
            return coupon_id
    except Exception as exc:
        if not _is_missing_coupon_error(exc):
            raise

    try:
        stripe.Coupon.create(
            id=coupon_id,
            percent_off=max(1, min(int(percent_off), 100)),
            duration='forever',
            name=f'TRAXION {plan} {int(percent_off)}%'.replace('pro_10k', 'Pro'),
            metadata={'source': 'traxion_user_plan_discount', 'plan': plan},
        )
    except Exception as exc:
        # Two simultaneous checkouts can race while creating the same deterministic
        # coupon. If another request won the race, a retrieve proves it exists;
        # otherwise preserve the original Stripe error.
        try:
            coupon = stripe.Coupon.retrieve(coupon_id)
            if getattr(coupon, 'valid', True):
                return coupon_id
        except Exception:
            pass
        raise exc
    return coupon_id


def _period_end(subscription) -> datetime | None:
    value = getattr(subscription, 'current_period_end', None)
    if value:
        return datetime.fromtimestamp(int(value), UTC)
    items = getattr(getattr(subscription, 'items', None), 'data', []) or []
    ends = [getattr(item, 'current_period_end', None) for item in items]
    ends = [int(x) for x in ends if x]
    return datetime.fromtimestamp(max(ends), UTC) if ends else None


async def checkout(
    *,
    customer_id: str | None,
    customer_email: str | None,
    user_id: str,
    plan: str,
    billing_period: str = 'monthly',
    discount_percent: int = 0,
) -> str:
    price = price_for(plan, billing_period)
    if not price:
        raise ValueError(f'Stripe price is not configured for {plan}/{billing_period}')
    kwargs = dict(
        mode='subscription', line_items=[{'price': price, 'quantity': 1}],
        success_url=f'{settings.PUBLIC_APP_URL}/billing?checkout=success', cancel_url=f'{settings.PUBLIC_APP_URL}/billing',
        client_reference_id=user_id,
        metadata={
            'user_id': user_id,
            'plan': plan,
            'billing_period': billing_period,
            'personal_discount_pct': str(max(0, min(int(discount_percent), 100))),
        },
        subscription_data={'metadata': {
            'user_id': user_id,
            'plan': plan,
            'billing_period': billing_period,
            'personal_discount_pct': str(max(0, min(int(discount_percent), 100))),
        }},
    )
    if discount_percent > 0:
        coupon_id = await asyncio.to_thread(_ensure_personal_coupon, plan, discount_percent)
        kwargs['discounts'] = [{'coupon': coupon_id}]
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
