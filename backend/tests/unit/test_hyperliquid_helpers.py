import pytest
from eth_account import Account

from app.adapters.hyperliquid import HyperliquidAdapter, deterministic_cloid, fill_event_id, parse_order_response


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


@pytest.mark.asyncio
async def test_verify_agent_rejects_declared_address_key_mismatch():
    main = Account.create()
    agent = Account.create()
    wrong_agent = Account.create()
    adapter = HyperliquidAdapter(None)  # mismatch is rejected before any network/rate-limit call
    with pytest.raises(ValueError, match='does not match'):
        await adapter.verify_agent(main.address, agent.key.hex(), wrong_agent.address)
