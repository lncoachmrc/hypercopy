from types import SimpleNamespace

import pytest
from pydantic import ValidationError

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
