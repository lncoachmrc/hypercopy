from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException

from app.api import billing
from app.models.entities import Subscription


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Db:
    def __init__(self, sub=None):
        self.sub = sub
        self.committed = False

    async def execute(self, _query):
        return _Result(self.sub)

    async def get(self, _model, key):
        return SimpleNamespace(slug=key)

    def add(self, value):
        if isinstance(value, Subscription):
            self.sub = value

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_100_percent_discount_activates_plan_without_stripe(monkeypatch):
    user = SimpleNamespace(id=uuid.uuid4(), email='user@example.com')
    db = _Db()

    async def discount(_db, _user_id, _plan):
        return 100

    async def no_audit(*_args, **_kwargs):
        return None

    async def entitlement(_db, _user):
        return {'entitled': True, 'status': 'complimentary', 'plan': 'starter'}

    monkeypatch.setattr(billing, 'discount_percent_for', discount)
    monkeypatch.setattr(billing, 'audit', no_audit)
    monkeypatch.setattr(billing, 'entitlement', entitlement)
    monkeypatch.setattr(billing.stripe_client, 'checkout', lambda **_kwargs: pytest.fail('Stripe must not be called'))

    result = await billing.activate_complimentary({'plan': 'starter'}, user=user, db=db)

    assert result['entitled'] is True
    assert db.sub is not None
    assert db.sub.plan_slug == 'starter'
    assert db.sub.status == 'complimentary'
    assert db.sub.period_end is None
    assert db.sub.trial_end is None
    assert db.committed is True


@pytest.mark.asyncio
async def test_direct_activation_requires_exact_100_percent_discount(monkeypatch):
    user = SimpleNamespace(id=uuid.uuid4(), email=None)
    db = _Db()

    async def discount(_db, _user_id, _plan):
        return 99

    monkeypatch.setattr(billing, 'discount_percent_for', discount)

    with pytest.raises(HTTPException) as exc:
        await billing.activate_complimentary({'plan': 'plus'}, user=user, db=db)

    assert exc.value.status_code == 409
    assert db.sub is None
    assert db.committed is False


@pytest.mark.asyncio
async def test_checkout_refuses_100_percent_discount_and_skips_stripe(monkeypatch):
    user = SimpleNamespace(id=uuid.uuid4(), email=None)
    db = _Db()

    async def discount(_db, _user_id, _plan):
        return 100

    monkeypatch.setattr(billing, 'discount_percent_for', discount)
    monkeypatch.setattr(billing.stripe_client, 'plan_configured', lambda *_args: True)

    async def fail_checkout(**_kwargs):
        pytest.fail('Stripe checkout must not run for a 100% discount')

    monkeypatch.setattr(billing.stripe_client, 'checkout', fail_checkout)

    with pytest.raises(HTTPException) as exc:
        await billing.create_checkout({'plan': 'pro_10k', 'billing_period': 'monthly'}, user=user, db=db)

    assert exc.value.status_code == 409
    assert 'complimentary' in str(exc.value.detail).lower()
