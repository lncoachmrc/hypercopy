from contextlib import asynccontextmanager
from decimal import Decimal

import pytest
from eth_account import Account

from app.adapters.hyperliquid import AssetSpec, HyperliquidAdapter


class FakeExchange:
    def __init__(self, events):
        self.events = events

    def set_expires_after(self, _value):
        return None

    def order(
        self,
        asset,
        is_buy,
        size,
        price,
        order_type,
        *,
        reduce_only,
        cloid,
    ):
        self.events.append("exchange")
        return {
            "status": "ok",
            "response": {
                "data": {
                    "statuses": [
                        {
                            "filled": {
                                "oid": 123,
                                "totalSz": str(size),
                                "avgPx": str(price),
                            }
                        }
                    ]
                }
            },
        }


async def _adapter(monkeypatch, events):
    @asynccontextmanager
    async def fake_signer_action_lock(_signer_address):
        yield

    monkeypatch.setattr(
        "app.adapters.hyperliquid.signer_action_lock",
        fake_signer_action_lock,
    )

    account = Account.create()
    adapter = HyperliquidAdapter(None, network="testnet")

    async def fake_asset_spec(asset):
        return AssetSpec(
            asset,
            sz_decimals=5,
            max_leverage=20,
        )

    async def no_strategy_intent(**_kwargs):
        events.append("strategy")
        return None

    monkeypatch.setattr(
        "app.services.strategy_intents.current_strategy_intent_for_cloid",
        no_strategy_intent,
    )
    monkeypatch.setattr(
        adapter,
        "asset_spec",
        fake_asset_spec,
    )
    monkeypatch.setattr(
        adapter,
        "_exchange",
        lambda _local, _account: FakeExchange(events),
    )

    return adapter, account.key.hex()


@pytest.mark.asyncio
async def test_profit_revalidation_runs_after_strategy_fence_before_exchange(
    monkeypatch,
):
    events = []
    adapter, private_key = await _adapter(
        monkeypatch,
        events,
    )

    async def revalidate():
        events.append("profit-revalidation")

    outcome = await adapter.place_ioc(
        account_address="0x" + "22" * 20,
        private_key=private_key,
        asset="BTC",
        is_buy=False,
        size=Decimal("0.01"),
        mark_price=Decimal("100000"),
        slippage_bps=25,
        reduce_only=True,
        cloid="0x" + "33" * 16,
        before_submit=revalidate,
    )

    assert outcome.state == "FILLED"
    assert events == [
        "strategy",
        "profit-revalidation",
        "exchange",
    ]


@pytest.mark.asyncio
async def test_profit_revalidation_refusal_never_reaches_exchange(
    monkeypatch,
):
    events = []
    adapter, private_key = await _adapter(
        monkeypatch,
        events,
    )

    async def revalidate():
        events.append("profit-revalidation")
        raise RuntimeError(
            "Residual net PnL is no longer strictly positive"
        )

    outcome = await adapter.place_ioc(
        account_address="0x" + "22" * 20,
        private_key=private_key,
        asset="BTC",
        is_buy=False,
        size=Decimal("0.01"),
        mark_price=Decimal("100000"),
        slippage_bps=25,
        reduce_only=True,
        cloid="0x" + "44" * 16,
        before_submit=revalidate,
    )

    assert outcome.state == "CANCELED"
    assert "profit" in (outcome.reason or "").lower()
    assert outcome.raw is not None
    assert outcome.raw.get("exchange_action_sent") is False

    assert events == [
        "strategy",
        "profit-revalidation",
    ]


@pytest.mark.asyncio
async def test_profit_revalidation_data_failure_is_definitive_local_cancel(
    monkeypatch,
):
    events = []
    adapter, private_key = await _adapter(
        monkeypatch,
        events,
    )

    async def revalidate():
        events.append("profit-revalidation")
        raise ValueError(
            "Profit-exit economics are incomplete"
        )

    outcome = await adapter.place_ioc(
        account_address="0x" + "22" * 20,
        private_key=private_key,
        asset="BTC",
        is_buy=False,
        size=Decimal("0.01"),
        mark_price=Decimal("100000"),
        slippage_bps=25,
        reduce_only=True,
        cloid="0x" + "55" * 16,
        before_submit=revalidate,
    )

    assert outcome.state == "CANCELED"
    assert outcome.state != "UNKNOWN"
    assert outcome.raw is not None
    assert outcome.raw.get("exchange_action_sent") is False
    assert "exchange" not in events


@pytest.mark.asyncio
async def test_existing_ioc_without_extra_callback_keeps_existing_semantics(
    monkeypatch,
):
    events = []
    adapter, private_key = await _adapter(
        monkeypatch,
        events,
    )

    outcome = await adapter.place_ioc(
        account_address="0x" + "22" * 20,
        private_key=private_key,
        asset="BTC",
        is_buy=False,
        size=Decimal("0.01"),
        mark_price=Decimal("100000"),
        slippage_bps=25,
        reduce_only=True,
        cloid="0x" + "66" * 16,
    )

    assert outcome.state == "FILLED"
    assert events == [
        "strategy",
        "exchange",
    ]
