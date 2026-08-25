from types import SimpleNamespace

import pytest

from app.adapters import stripe_client
from app.services.plan_discounts import apply_percent_discount


def test_apply_percent_discount_bounds_and_rounding():
    assert apply_percent_discount(33, 50) == 16.5
    assert apply_percent_discount(19.5, 100) == 0
    assert apply_percent_discount(12, -10) == 12
    assert apply_percent_discount(12, 150) == 0


def test_personal_coupon_id_is_deterministic():
    assert stripe_client.personal_coupon_id('starter', 50) == 'traxion-starter-50-pct'
    assert stripe_client.personal_coupon_id('pro_10k', 100) == 'traxion-pro_10k-100-pct'


@pytest.mark.asyncio
async def test_checkout_applies_server_side_personal_coupon(monkeypatch):
    class MissingCoupon(Exception):
        http_status = 404

    created_coupons = []
    checkout_kwargs = {}

    monkeypatch.setattr(stripe_client, 'price_for', lambda plan, period: 'price_test')
    monkeypatch.setattr(stripe_client.stripe.Coupon, 'retrieve', lambda coupon_id: (_ for _ in ()).throw(MissingCoupon('No such coupon')))
    monkeypatch.setattr(stripe_client.stripe.Coupon, 'create', lambda **kwargs: created_coupons.append(kwargs) or SimpleNamespace(id=kwargs['id'], valid=True))

    def create_session(**kwargs):
        checkout_kwargs.update(kwargs)
        return SimpleNamespace(url='https://checkout.example/session')

    monkeypatch.setattr(stripe_client.stripe.checkout.Session, 'create', create_session)

    url = await stripe_client.checkout(
        customer_id=None,
        customer_email='user@example.com',
        user_id='user-1',
        plan='plus',
        billing_period='yearly',
        discount_percent=50,
    )

    assert url == 'https://checkout.example/session'
    assert created_coupons[0]['id'] == 'traxion-plus-50-pct'
    assert created_coupons[0]['percent_off'] == 50
    assert created_coupons[0]['duration'] == 'forever'
    assert checkout_kwargs['discounts'] == [{'coupon': 'traxion-plus-50-pct'}]
    assert checkout_kwargs['metadata']['personal_discount_pct'] == '50'
    assert checkout_kwargs['subscription_data']['metadata']['personal_discount_pct'] == '50'


@pytest.mark.asyncio
async def test_checkout_without_discount_does_not_add_coupon(monkeypatch):
    checkout_kwargs = {}
    monkeypatch.setattr(stripe_client, 'price_for', lambda plan, period: 'price_test')
    monkeypatch.setattr(stripe_client.stripe.Coupon, 'retrieve', lambda coupon_id: pytest.fail('coupon lookup should not run'))
    monkeypatch.setattr(stripe_client.stripe.Coupon, 'create', lambda **kwargs: pytest.fail('coupon create should not run'))

    def create_session(**kwargs):
        checkout_kwargs.update(kwargs)
        return SimpleNamespace(url='https://checkout.example/session')

    monkeypatch.setattr(stripe_client.stripe.checkout.Session, 'create', create_session)

    await stripe_client.checkout(
        customer_id='cus_123',
        customer_email=None,
        user_id='user-1',
        plan='starter',
        discount_percent=0,
    )

    assert 'discounts' not in checkout_kwargs
    assert checkout_kwargs['customer'] == 'cus_123'
    assert checkout_kwargs['metadata']['personal_discount_pct'] == '0'
