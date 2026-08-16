from decimal import Decimal

import pytest
from eth_account import Account

from app.adapters.hyperliquid import HyperliquidAdapter, deterministic_cloid, parse_order_response


def test_cloid_is_exactly_128_bits_and_deterministic():
    a=deterministic_cloid('job-1','o')
    assert a==deterministic_cloid('job-1','o')
    assert a.startswith('0x') and len(a)==34
    int(a[2:],16)


def test_parse_filled_order_response():
    r=parse_order_response({'response':{'data':{'statuses':[{'filled':{'oid':7,'totalSz':'0.1','avgPx':'60000'}}]}}})
    assert r.state=='FILLED' and str(r.filled_size)=='0.1'


def test_parse_rejection():
    r=parse_order_response({'response':{'data':{'statuses':[{'error':'Insufficient margin'}]}})
    assert r.state=='REJECTED'


def test_adapter_selects_network_specific_endpoints():
    main = HyperliquidAdapter(None, network='mainnet')
    test = HyperliquidAdapter(None, network='testnet')
    assert main.api_url == 'https://api.hyperliquid.xyz'
    assert main.ws_url == 'wss://api.hyperliquid.xyz/ws'
    assert test.api_url == 'https://api.hyperliquid-testnet.xyz'
    assert test.ws_url == 'wss://api.hyperliquid-testnet.xyz/ws'


@pytest.mark.asyncio
async def test_verify_agent_rejects_declared_address_key_mismatch():
    main = Account.create()
    agent = Account.create()
    wrong_agent = Account.create()
    adapter = HyperliquidAdapter(None)
    with pytest.raises(ValueError, match='does not match'):
        await adapter.verify_agent(main.address, agent.key.hex(), wrong_agent.address)


@pytest.mark.asyncio
async def test_unified_account_uses_spot_usdc_for_equity(monkeypatch):
    adapter = HyperliquidAdapter(None, network='testnet')

    async def user_state(*_args, **_kwargs):
        return {
            'marginSummary': {'accountValue': '0', 'totalMarginUsed': '0'},
            'withdrawable': '0',
            'assetPositions': [],
        }

    async def abstraction(*_args, **_kwargs):
        return 'unifiedAccount'

    async def spot_state(*_args, **_kwargs):
        return {'balances': [{'coin': 'USDC', 'token': 0, 'total': '498.99', 'hold': '0'}]}

    monkeypatch.setattr(adapter, 'user_state', user_state)
    monkeypatch.setattr(adapter, 'user_abstraction', abstraction)
    monkeypatch.setattr(adapter, 'spot_user_state', spot_state)

    snap = await adapter.account_snapshot('0x0000000000000000000000000000000000000001')
    assert snap.abstraction == 'unifiedAccount'
    assert snap.account_value == Decimal('498.99')
    assert snap.free_margin == Decimal('498.99')


@pytest.mark.asyncio
async def test_unified_account_includes_unrealized_pnl(monkeypatch):
    adapter = HyperliquidAdapter(None, network='testnet')

    async def user_state(*_args, **_kwargs):
        return {
            'marginSummary': {'accountValue': '0', 'totalMarginUsed': '50'},
            'assetPositions': [
                {'position': {'coin': 'BTC', 'szi': '0.01', 'unrealizedPnl': '12.34'}}
            ],
        }

    async def abstraction(*_args, **_kwargs):
        return 'unifiedAccount'

    async def spot_state(*_args, **_kwargs):
        return {'balances': [{'coin': 'USDC', 'token': 0, 'total': '500', 'hold': '5'}]}

    monkeypatch.setattr(adapter, 'user_state', user_state)
    monkeypatch.setattr(adapter, 'user_abstraction', abstraction)
    monkeypatch.setattr(adapter, 'spot_user_state', spot_state)

    snap = await adapter.account_snapshot('0x0000000000000000000000000000000000000001')
    assert snap.account_value == Decimal('512.34')
    assert snap.free_margin == Decimal('457.34')


@pytest.mark.asyncio
async def test_standard_account_keeps_perp_equity(monkeypatch):
    adapter = HyperliquidAdapter(None, network='testnet')

    async def user_state(*_args, **_kwargs):
        return {
            'marginSummary': {'accountValue': '321.50', 'totalMarginUsed': '21.50'},
            'withdrawable': '300',
            'assetPositions': [],
        }

    async def abstraction(*_args, **_kwargs):
        return 'disabled'

    monkeypatch.setattr(adapter, 'user_state', user_state)
    monkeypatch.setattr(adapter, 'user_abstraction', abstraction)

    snap = await adapter.account_snapshot('0x0000000000000000000000000000000000000001')
    assert snap.account_value == Decimal('321.50')
    assert snap.free_margin == Decimal('300')
    assert snap.spot_state is None


@pytest.mark.asyncio
async def test_portfolio_margin_is_rejected_until_fully_supported(monkeypatch):
    adapter = HyperliquidAdapter(None, network='testnet')

    async def user_state(*_args, **_kwargs):
        return {'marginSummary': {'accountValue': '0'}, 'assetPositions': []}

    async def abstraction(*_args, **_kwargs):
        return 'portfolioMargin'

    monkeypatch.setattr(adapter, 'user_state', user_state)
    monkeypatch.setattr(adapter, 'user_abstraction', abstraction)

    with pytest.raises(ValueError, match='Portfolio Margin'):
        await adapter.account_snapshot('0x0000000000000000000000000000000000000001')
