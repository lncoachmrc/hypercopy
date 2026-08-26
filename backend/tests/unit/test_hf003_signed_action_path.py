import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest
from eth_account import Account

from app.adapters.hyperliquid import HyperliquidAdapter
from app.engine.sizing import AssetSpec


class _FakeExchange:
    def __init__(self):
        self.expires_after = None

    def set_expires_after(self, value):
        self.expires_after = value

    def update_leverage(self, leverage, asset, is_cross):
        return {"status": "ok", "leverage": leverage, "asset": asset, "is_cross": is_cross}

    def order(self, asset, is_buy, size, px, order_type, *, reduce_only, cloid):
        return {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {"statuses": [{"filled": {"oid": 123, "totalSz": str(size), "avgPx": str(px)}}]},
            },
        }


@pytest.mark.asyncio
async def test_all_current_signed_adapter_actions_use_public_signer_lock(monkeypatch):
    account = Account.create()
    private_key = account.key.hex()
    expected_signer = account.address.lower()
    lock_entries = []

    @asynccontextmanager
    async def fake_signer_lock(signer_address: str):
        lock_entries.append(signer_address.lower())
        yield

    adapter = HyperliquidAdapter(None, network="testnet")
    fake_exchange = _FakeExchange()

    async def fake_asset_spec(asset: str):
        return AssetSpec(asset, sz_decimals=5, max_leverage=20)

    monkeypatch.setattr("app.adapters.hyperliquid.signer_action_lock", fake_signer_lock)
    monkeypatch.setattr(adapter, "asset_spec", fake_asset_spec)
    monkeypatch.setattr(adapter, "_exchange", lambda local, account_address: fake_exchange)

    leverage_result = await adapter.update_leverage(
        account_address="0x" + "22" * 20,
        private_key=private_key,
        asset="BTC",
        leverage=3,
        is_cross=True,
    )
    assert leverage_result["status"] == "ok"

    outcome = await adapter.place_ioc(
        account_address="0x" + "22" * 20,
        private_key=private_key,
        asset="BTC",
        is_buy=True,
        size=Decimal("0.01"),
        mark_price=Decimal("100000"),
        slippage_bps=25,
        reduce_only=False,
        cloid="0x" + "33" * 16,
    )
    assert outcome.state == "FILLED"
    assert lock_entries == [expected_signer, expected_signer]


@pytest.mark.asyncio
async def test_signed_call_holds_lock_until_inflight_action_finishes_after_cancel(monkeypatch):
    entered = asyncio.Event()
    released = asyncio.Event()
    call_started = asyncio.Event()
    allow_finish = asyncio.Event()

    @asynccontextmanager
    async def fake_signer_lock(signer_address: str):
        assert signer_address == "0x" + "11" * 20
        entered.set()
        try:
            yield
        finally:
            released.set()

    adapter = HyperliquidAdapter(None, network="testnet")

    async def fake_call(func, *args):
        call_started.set()
        await allow_finish.wait()
        return {"status": "ok"}

    monkeypatch.setattr("app.adapters.hyperliquid.signer_action_lock", fake_signer_lock)
    monkeypatch.setattr(adapter, "_call", fake_call)

    task = asyncio.create_task(
        adapter._signed_call("0x" + "11" * 20, lambda: {"status": "ok"})
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    await asyncio.wait_for(call_started.wait(), timeout=1)

    task.cancel()
    await asyncio.sleep(0)
    assert not released.is_set()
    assert not task.done()

    allow_finish.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert released.is_set()
