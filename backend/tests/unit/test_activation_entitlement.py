from decimal import Decimal
from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException

from app.api import activation


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _ActivationDb:
    def __init__(self):
        self.results = [
            None,  # RiskState
            SimpleNamespace(account_address='0xfollower'),  # TradingAccount
            None,  # pending CopyJob
        ]

    async def execute(self, _query):
        return _Result(self.results.pop(0))


class _FakeHyperliquidAdapter:
    def __init__(self, _limiter, network=None):
        self.network = network

    async def account_snapshot(self, address, *, priority=None):
        if address == '0xmaster':
            return SimpleNamespace(
                perp_state={'assetPositions': []},
                account_value=Decimal('5000'),
            )
        return SimpleNamespace(
            perp_state={'assetPositions': []},
            account_value=Decimal('3200'),
        )

    async def mids(self):
        return {}


@pytest.mark.asyncio
async def test_activation_uses_fresh_follower_equity_for_entitlement(monkeypatch):
    user = SimpleNamespace(id=uuid.uuid4())
    db = _ActivationDb()
    captured = {}

    async def network_state(_db, _user_id):
        return SimpleNamespace(network='mainnet')

    async def live_allowed(_db, _network):
        return True

    async def capped_entitlement(_db, _user, *, portfolio_equity_override=None):
        captured['equity'] = portfolio_equity_override
        return {
            'entitled': False,
            'status': 'complimentary',
            'plan': 'starter',
            'commercial_plan': 'starter',
            'portfolio_limit_exceeded': True,
            'portfolio_equity': float(portfolio_equity_override),
            'portfolio_limit_usd': 2500,
        }

    async def causal_order(*, required=False):
        assert required is True
        return 41

    monkeypatch.setattr(activation, 'user_network_state', network_state)
    monkeypatch.setattr(activation, 'live_trading_allowed', live_allowed)
    monkeypatch.setattr(activation, 'entitlement', capped_entitlement)
    monkeypatch.setattr(activation, 'master_snapshot_started_order', causal_order)
    monkeypatch.setattr(activation, 'HyperliquidAdapter', _FakeHyperliquidAdapter)
    monkeypatch.setattr(activation, '_limiter', lambda: None)
    monkeypatch.setattr(activation.settings, 'HYPERLIQUID_MASTER_ADDRESS', '0xmaster')

    with pytest.raises(HTTPException) as exc:
        await activation.resume_copy_immediate(user=user, db=db)

    assert captured['equity'] == Decimal('3200')
    assert exc.value.status_code == 409
    assert '$3200.00' in exc.value.detail
    assert '$2500.00' in exc.value.detail


def test_activation_allocates_causal_order_before_master_snapshot() -> None:
    source = __import__('inspect').getsource(activation.resume_copy_immediate)
    order_index = source.index('master_snapshot_started_order(required=True)')
    snapshot_index = source.index('master_hl.account_snapshot(')
    assert order_index < snapshot_index
    assert 'observed_master_mids(await master_hl.mids(), snapshot_started_order)' in source


def test_activation_entitlement_error_explains_missing_plan():
    assert activation._activation_entitlement_error({
        'entitled': False,
        'status': 'none',
        'portfolio_limit_exceeded': False,
    }) == 'Activate a plan before activating the strategy.'


def test_activation_entitlement_error_allows_valid_plan():
    assert activation._activation_entitlement_error({'entitled': True}) is None
