from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api.user import _network_switch_blockers
from app.core.config import settings
from app.schemas.user import TradingNetworkIn
from app.services.execution import live_trading_allowed


class FlagDb:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    async def get(self, model, key):
        assert key == 'live_trading'
        return SimpleNamespace(enabled=self.enabled)


def test_trading_network_accepts_only_supported_networks():
    assert TradingNetworkIn(network='testnet').network == 'testnet'
    assert TradingNetworkIn(network='mainnet').network == 'mainnet'
    with pytest.raises(ValidationError):
        TradingNetworkIn(network='devnet')


def test_network_toggle_unlocks_when_operational_state_is_clean():
    assert _network_switch_blockers(
        copy_state='PAUSED',
        has_open_managed=False,
        has_pending_jobs=False,
        has_unresolved_execution=False,
    ) == []


def test_network_toggle_reports_only_real_operational_blockers():
    blockers = _network_switch_blockers(
        copy_state='ACTIVE',
        has_open_managed=True,
        has_pending_jobs=True,
        has_unresolved_execution=True,
    )
    assert [item['code'] for item in blockers] == ['pause', 'positions', 'jobs', 'executions']
    assert 'api_wallet' not in [item['code'] for item in blockers]


@pytest.mark.asyncio
async def test_testnet_never_depends_on_mainnet_live_gate(monkeypatch):
    monkeypatch.setattr(settings, 'ENABLE_LIVE_TRADING', False)
    assert await live_trading_allowed(FlagDb(False), 'testnet') is True


@pytest.mark.asyncio
async def test_mainnet_requires_environment_and_database_gates(monkeypatch):
    monkeypatch.setattr(settings, 'ENABLE_LIVE_TRADING', False)
    assert await live_trading_allowed(FlagDb(True), 'mainnet') is False

    monkeypatch.setattr(settings, 'ENABLE_LIVE_TRADING', True)
    assert await live_trading_allowed(FlagDb(False), 'mainnet') is False
    assert await live_trading_allowed(FlagDb(True), 'mainnet') is True
