from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from eth_account import Account

from app.adapters.hyperliquid import HyperliquidAdapter, _transient_read_error, deterministic_cloid, parse_order_response, position_configs
from app.adapters.ratelimit import Priority


def test_cloid_is_exactly_128_bits_and_deterministic():
    a=deterministic_cloid('job-1','o')
    assert a==deterministic_cloid('job-1','o')
    assert a.startswith('0x') and len(a)==34
    int(a[2:],16)


def test_parse_filled_order_response():
    r=parse_order_response({'response':{'data':{'statuses':[{'filled':{'oid':7,'totalSz':'0.1','avgPx':'60000'}}]}}})
    assert r.state=='FILLED' and str(r.filled_size)=='0.1'


def test_parse_rejection():
    r=parse_order_response({'response':{'data':{'statuses':[{'error':'Insufficient margin'}]}}})
    assert r.state=='REJECTED'


def test_transient_read_error_only_matches_retryable_transport_failures():
    assert _transient_read_error(RuntimeError('502 Bad Gateway')) is True
    assert _transient_read_error(RuntimeError('504 Gateway Timeout')) is True
    assert _transient_read_error(RuntimeError('connection reset by peer')) is True
    assert _transient_read_error(RuntimeError('Insufficient margin')) is False


def test_adapter_selects_network_specific_endpoints(monkeypatch):
    monkeypatch.setattr('app.adapters.hyperliquid.Info', MagicMock())
    main=HyperliquidAdapter(None, network='mainnet')
    test=HyperliquidAdapter(None, network='testnet')
    assert main.api_url=='https://api.hyperliquid.xyz'
    assert main.ws_url=='wss://api.hyperliquid.xyz/ws'
    assert test.api_url=='https://api.hyperliquid-testnet.xyz'
    assert test.ws_url=='wss://api.hyperliquid-testnet.xyz/ws'


def test_position_configs_extracts_master_leverage_and_margin_mode():
    configs=position_configs({
        'assetPositions':[
            {'position':{'coin':'BTC','szi':'0.00128','leverage':{'type':'cross','value':2}}},
            {'position':{'coin':'ETH','szi':'-0.01','leverage':{'type':'isolated','value':'5'}}},
        ]
    })
    assert configs['BTC'].leverage==2
    assert configs['BTC'].is_cross is True
    assert configs['ETH'].leverage==5
    assert configs['ETH'].is_cross is False


@pytest.mark.asyncio
async def test_safe_read_retries_transient_502_then_succeeds(monkeypatch):
    monkeypatch.setattr('app.adapters.hyperliquid.Info', MagicMock())
    monkeypatch.setattr('app.adapters.hyperliquid.settings.HL_SAFE_READ_RETRIES', 3)
    monkeypatch.setattr('app.adapters.hyperliquid.settings.HL_SAFE_READ_BACKOFF_SECONDS', 0)
    adapter=HyperliquidAdapter(None, network='testnet')
    adapter._metric_incr=AsyncMock()
    calls=0

    def flaky_read():
        nonlocal calls
        calls+=1
        if calls<3:
            raise RuntimeError('502 Bad Gateway')
        return {'ok':True}

    result=await adapter._read(flaky_read,weight=2,priority=Priority.MASTER_STATE,timeout=1)
    assert result=={'ok':True}
    assert calls==3
    assert adapter._metric_incr.await_count==2


@pytest.mark.asyncio
async def test_safe_read_does_not_retry_non_transient_failure(monkeypatch):
    monkeypatch.setattr('app.adapters.hyperliquid.Info', MagicMock())
    monkeypatch.setattr('app.adapters.hyperliquid.settings.HL_SAFE_READ_RETRIES', 3)
    monkeypatch.setattr('app.adapters.hyperliquid.settings.HL_SAFE_READ_BACKOFF_SECONDS', 0)
    adapter=HyperliquidAdapter(None, network='testnet')
    calls=0

    def bad_read():
        nonlocal calls
        calls+=1
        raise RuntimeError('semantic validation failure')

    with pytest.raises(RuntimeError,match='semantic validation failure'):
        await adapter._read(bad_read,weight=2,priority=Priority.MASTER_STATE,timeout=1)
    assert calls==1


@pytest.mark.asyncio
async def test_verify_agent_rejects_declared_address_key_mismatch(monkeypatch):
    monkeypatch.setattr('app.adapters.hyperliquid.Info', MagicMock())
    main=Account.create()
    agent=Account.create()
    wrong_agent=Account.create()
    adapter=HyperliquidAdapter(None)
    with pytest.raises(ValueError, match='does not match'):
        await adapter.verify_agent(main.address, agent.key.hex(), wrong_agent.address)


@pytest.mark.asyncio
async def test_unified_account_uses_spot_usdc_for_equity(monkeypatch):
    monkeypatch.setattr('app.adapters.hyperliquid.Info', MagicMock())
    adapter=HyperliquidAdapter(None, network='testnet')

    async def user_state(*_args, **_kwargs):
        return {
            'marginSummary':{'accountValue':'0','totalMarginUsed':'0'},
            'withdrawable':'0',
            'assetPositions':[],
        }

    async def abstraction(*_args, **_kwargs):
        return 'unifiedAccount'

    async def spot_state(*_args, **_kwargs):
        return {'balances':[{'coin':'USDC','token':0,'total':'498.99','hold':'0'}]}

    monkeypatch.setattr(adapter, 'user_state', user_state)
    monkeypatch.setattr(adapter, 'user_abstraction', abstraction)
    monkeypatch.setattr(adapter, 'spot_user_state', spot_state)

    snap=await adapter.account_snapshot('0x0000000000000000000000000000000000000001')
    assert snap.abstraction=='unifiedAccount'
    assert snap.account_value==Decimal('498.99')
    assert snap.free_margin==Decimal('498.99')


@pytest.mark.asyncio
async def test_unified_account_includes_unrealized_pnl(monkeypatch):
    monkeypatch.setattr('app.adapters.hyperliquid.Info', MagicMock())
    adapter=HyperliquidAdapter(None, network='testnet')

    async def user_state(*_args, **_kwargs):
        return {
            'marginSummary':{'accountValue':'0','totalMarginUsed':'50'},
            'assetPositions':[
                {'position':{'coin':'BTC','szi':'0.01','unrealizedPnl':'12.34'}}
            ],
        }

    async def abstraction(*_args, **_kwargs):
        return 'unifiedAccount'

    async def spot_state(*_args, **_kwargs):
        return {'balances':[{'coin':'USDC','token':0,'total':'500','hold':'5'}]}

    monkeypatch.setattr(adapter, 'user_state', user_state)
    monkeypatch.setattr(adapter, 'user_abstraction', abstraction)
    monkeypatch.setattr(adapter, 'spot_user_state', spot_state)

    snap=await adapter.account_snapshot('0x0000000000000000000000000000000000000001')
    assert snap.account_value==Decimal('512.34')
    assert snap.free_margin==Decimal('457.34')


@pytest.mark.asyncio
async def test_standard_account_keeps_perp_equity(monkeypatch):
    monkeypatch.setattr('app.adapters.hyperliquid.Info', MagicMock())
    adapter=HyperliquidAdapter(None, network='testnet')

    async def user_state(*_args, **_kwargs):
        return {
            'marginSummary':{'accountValue':'321.50','totalMarginUsed':'21.50'},
            'withdrawable':'300',
            'assetPositions':[],
        }

    async def abstraction(*_args, **_kwargs):
        return 'disabled'

    monkeypatch.setattr(adapter, 'user_state', user_state)
    monkeypatch.setattr(adapter, 'user_abstraction', abstraction)

    snap=await adapter.account_snapshot('0x0000000000000000000000000000000000000001')
    assert snap.account_value==Decimal('321.50')
    assert snap.free_margin==Decimal('300')
    assert snap.spot_state is None


@pytest.mark.asyncio
async def test_portfolio_margin_is_rejected_until_fully_supported(monkeypatch):
    monkeypatch.setattr('app.adapters.hyperliquid.Info', MagicMock())
    adapter=HyperliquidAdapter(None, network='testnet')

    async def user_state(*_args, **_kwargs):
        return {'marginSummary':{'accountValue':'0'},'assetPositions':[]}

    async def abstraction(*_args, **_kwargs):
        return 'portfolioMargin'

    monkeypatch.setattr(adapter, 'user_state', user_state)
    monkeypatch.setattr(adapter, 'user_abstraction', abstraction)

    with pytest.raises(ValueError, match='Portfolio Margin'):
        await adapter.account_snapshot('0x0000000000000000000000000000000000000001')
