from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException

from app.api import activation


@pytest.mark.asyncio
async def test_activation_rejects_missing_entitlement_before_operational_preflight(monkeypatch):
    user = SimpleNamespace(id=uuid.uuid4())
    db = object()

    async def network_state(_db, _user_id):
        return SimpleNamespace(network='mainnet')

    async def live_allowed(_db, _network):
        return True

    async def no_entitlement(_db, _user):
        return {
            'entitled': False,
            'status': 'none',
            'plan': None,
            'commercial_plan': None,
            'portfolio_limit_exceeded': False,
        }

    monkeypatch.setattr(activation, 'user_network_state', network_state)
    monkeypatch.setattr(activation, 'live_trading_allowed', live_allowed)
    monkeypatch.setattr(activation, 'entitlement', no_entitlement)

    with pytest.raises(HTTPException) as exc:
        await activation.resume_copy_immediate(user=user, db=db)

    assert exc.value.status_code == 409
    assert exc.value.detail == 'Activate a plan before activating the strategy.'


def test_activation_entitlement_error_explains_portfolio_limit():
    message = activation._activation_entitlement_error({
        'entitled': False,
        'status': 'complimentary',
        'portfolio_limit_exceeded': True,
        'portfolio_equity': 3200,
        'portfolio_limit_usd': 2500,
    })

    assert message is not None
    assert '$3200.00' in message
    assert '$2500.00' in message
    assert 'Choose a plan that covers the account' in message


def test_activation_entitlement_error_allows_valid_plan():
    assert activation._activation_entitlement_error({'entitled': True}) is None
