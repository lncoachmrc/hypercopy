"""RISEx shadow Profit Exit testnet adapter: no writes, no real PnL claims."""
import inspect
from decimal import Decimal as D

import pytest

from app.services import ai_profit_exit_decision as decision
from app.services.ai_profit_exit_risex_shadow import (
    RISEX_RESEARCH_TAKER_FEE_RATE,
    RISEX_SHADOW_BASIS,
    collect_risex_shadow_profit_exit_economics,
)


class FakeReadOnlyRISEx:
    public_read_only = True

    def __init__(self, *, mark="100", bid="99.8", ask="100.2", failed=False):
        self.calls = []
        self.failed = failed
        self.mark, self.bid, self.ask = mark, bid, ask

    async def get_json(self, path, *, params=None):
        self.calls.append((path, params))
        if self.failed:
            raise RuntimeError("provider unavailable")
        if path == "/v1/markets":
            return {"data": {"markets": [{
                "market_id": "17", "display_name": "BTC/USDC",
                "base_asset_symbol": "BTC", "active": True,
                "mark_price": self.mark,
                "config": {
                    "step_size": "0.001", "step_price": "0.1",
                    "min_order_size": "0.01", "unlocked": True,
                },
            }]}}
        if path == "/v1/orderbook":
            assert params == {"market_id": 17, "limit": 1}
            return {"data": {"bids": [{"price": self.bid}],
                             "asks": [{"price": self.ask}]}}
        raise AssertionError("Unexpected RISEx endpoint: " + path)

    async def post_json(self, *_args, **_kwargs):
        raise AssertionError("Order path forbidden during shadow research")


MASTER_SHORT = {"assetPositions": [{"position": {
    "coin": "BTC", "szi": "-1", "entryPx": "110",
}}]}
MASTER_LONG = {"assetPositions": [{"position": {
    "coin": "BTC", "szi": "1", "entryPx": "90",
}}]}


@pytest.mark.asyncio
async def test_risex_short_uses_ask_conservative_slippage_and_assumed_costs():
    client = FakeReadOnlyRISEx()
    row = await collect_risex_shadow_profit_exit_economics(
        client, asset="BTC", shadow_position=D("-0.5"),
        master_perp_state=MASTER_SHORT, slippage_bps=30, network="testnet",
    )
    assert row.complete
    assert row.eligible
    assert row.basis == RISEX_SHADOW_BASIS
    assert row.pnl_complete is False
    assert row.funding_model == "not_modeled"
    assert row.executable_exit_price == D("100.6")  # ask + 30bps, ceil to ticks
    assert row.taker_fee_rate == RISEX_RESEARCH_TAKER_FEE_RATE
    assert row.economics is not None
    assert row.economics.gross_price_pnl == D("4.70")
    assert row.economics.residual_funding == D("0")
    assert row.economics.net_pnl < row.economics.gross_price_pnl
    assert [c[0] for c in client.calls] == ["/v1/markets", "/v1/orderbook"]


@pytest.mark.asyncio
async def test_risex_long_uses_bid_downward_rounding_but_not_retention():
    client = FakeReadOnlyRISEx()
    row = await collect_risex_shadow_profit_exit_economics(
        client, asset="BTC", shadow_position=D("0.5"),
        master_perp_state=MASTER_LONG, slippage_bps=20, network="testnet",
    )
    assert row.complete
    assert row.executable_exit_price == D("99.6")
    assert row.economics.net_pnl > 0


@pytest.mark.parametrize("network", ["mainnet", "invalid"])
@pytest.mark.asyncio
async def test_non_testnet_never_issues_provider_io(network):
    client = FakeReadOnlyRISEx()
    result = await collect_risex_shadow_profit_exit_economics(
        client, asset="BTC", shadow_position=D("-1"),
        master_perp_state=MASTER_SHORT, slippage_bps=10, network=network,
    )
    assert not result.eligible
    assert not client.calls


@pytest.mark.parametrize("size,slippage,master", [
    ("0", 20, MASTER_SHORT),
    ("nan", 20, MASTER_SHORT),
    ("-0.001", 20, MASTER_SHORT),
    ("-0.5005", 20, MASTER_SHORT),
    ("-0.5", -1, MASTER_SHORT),
    ("-0.5", 10000, MASTER_SHORT),
    ("-0.5", 10, MASTER_LONG),
])
@pytest.mark.asyncio
async def test_invalid_shadow_economics_fail_closed(size, slippage, master):
    client = FakeReadOnlyRISEx()
    result = await collect_risex_shadow_profit_exit_economics(
        client, asset="BTC", shadow_position=D(size),
        master_perp_state=master, slippage_bps=slippage, network="testnet",
    )
    assert result.complete is False
    assert result.eligible is False
    assert result.economics is None


@pytest.mark.asyncio
async def test_unknown_or_failed_provider_fails_closed_without_fallback():
    for client in [FakeReadOnlyRISEx(failed=True), FakeReadOnlyRISEx(mark="NaN")]:
        result = await collect_risex_shadow_profit_exit_economics(
            client, asset="BTC", shadow_position=D("-1"),
            master_perp_state=MASTER_SHORT, slippage_bps=0, network="testnet",
        )
        assert not result.complete
        assert not result.eligible


@pytest.mark.asyncio
async def test_reject_authenticated_transport_even_before_get():
    client = FakeReadOnlyRISEx()
    client.public_read_only = False
    result = await collect_risex_shadow_profit_exit_economics(
        client, asset="BTC", shadow_position=D("-1"),
        master_perp_state=MASTER_SHORT, slippage_bps=10, network="testnet",
    )
    assert not result.complete
    assert not client.calls


def test_evaluator_uses_risex_account_not_legacy_hl_and_never_queues_risex():
    source = inspect.getsource(decision.evaluate_profit_exit_portfolio)
    assert ".outerjoin(TradingAccount" in source
    assert "RISExTradingAccount.user_id == user.id" in source
    assert "epoch.account_address.lower() != risex_account.account_address.lower()" in source
    assert "collect_risex_shadow_profit_exit_economics(" in source
    assert 'base_url="https://api.testnet.rise.trade"' in source
    block = source[source.index("operational = ("): source.index("job_id = profit_exit_job_id")]
    assert 'destination.provider == "hyperliquid"' in block
    assert "user.copy_state == CopyState.ACTIVE" in block
