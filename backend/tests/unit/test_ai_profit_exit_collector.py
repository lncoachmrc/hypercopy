from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.ai_profit_exit_collector import (
    collect_profit_exit_economics,
)


D = Decimal


def _snapshot(
    *,
    asset="BTC",
    size="1",
    entry="100",
):
    return SimpleNamespace(
        perp_state={
            "assetPositions": [
                {
                    "position": {
                        "coin": asset,
                        "szi": size,
                        "entryPx": entry,
                    }
                }
            ]
        }
    )


def _fill(
    *,
    coin="BTC",
    time=1000,
    start="0",
    side="B",
    size="1",
    fee="0.10",
):
    return {
        "coin": coin,
        "time": time,
        "startPosition": start,
        "side": side,
        "sz": size,
        "fee": fee,
    }


def _funding(
    *,
    coin="BTC",
    time=1500,
    position="1",
    usdc="-0.20",
):
    return {
        "time": time,
        "delta": {
            "coin": coin,
            "szi": position,
            "usdc": usdc,
        },
    }


class FakeHL:
    network = "mainnet"

    def __init__(
        self,
        *,
        snapshot=None,
        mids=None,
        fills=None,
        funding=None,
        fees=None,
        sz_decimals=4,
    ):
        self.snapshot = snapshot or _snapshot()
        self._mids = mids if mids is not None else {"BTC": "110"}
        self._fills = fills if fills is not None else [_fill()]
        self._funding = (
            funding if funding is not None else [_funding()]
        )
        self._fees = fees or {"userCrossRate": "0.001"}
        self.sz_decimals = sz_decimals
        self.calls = []

    async def account_snapshot(self, account, **kwargs):
        self.calls.append(("snapshot", account, kwargs))
        return self.snapshot

    async def mids(self, **kwargs):
        self.calls.append(("mids", kwargs))
        return self._mids

    async def asset_spec(self, asset):
        self.calls.append(("spec", asset))
        return SimpleNamespace(
            name=asset,
            sz_decimals=self.sz_decimals,
            max_leverage=50,
            only_isolated=False,
        )

    async def user_fills_by_time(
        self,
        account,
        start_ms,
        end_ms=None,
    ):
        self.calls.append(
            ("fills", account, start_ms, end_ms)
        )
        return self._fills

    async def user_funding_history(
        self,
        account,
        start_ms,
        end_ms=None,
    ):
        self.calls.append(
            ("funding", account, start_ms, end_ms)
        )
        return self._funding

    async def user_fees(self, account):
        self.calls.append(("fees", account))
        return self._fees


@pytest.mark.asyncio
async def test_collect_long_uses_conservative_ioc_limit_price():
    hl = FakeHL()

    observed = await collect_profit_exit_economics(
        hl,
        account_address="0x" + "11" * 20,
        asset="BTC",
        history_start_ms=500,
        history_end_ms=2000,
        slippage_bps=50,
    )

    assert observed.complete is True
    assert observed.asset == "BTC"
    assert observed.current_position == D("1")
    assert observed.entry_price == D("100")
    assert observed.mark_price == D("110")

    # Closing a long is a SELL:
    # 110 * (1 - 50bps) = 109.45.
    assert observed.executable_exit_price == D("109.45")
    assert observed.taker_fee_rate == D("0.001")

    assert observed.economics is not None
    assert observed.economics.complete is True
    assert observed.economics.eligible is True
    assert observed.economics.gross_price_pnl == D("9.45")

    # Entry fee 0.10 + funding paid 0.20 +
    # estimated exit fee 109.45 * 0.001.
    assert observed.economics.net_pnl == D("9.04055")


@pytest.mark.asyncio
async def test_collect_short_uses_conservative_buy_limit_price():
    hl = FakeHL(
        snapshot=_snapshot(
            size="-2",
            entry="100",
        ),
        mids={"BTC": "90"},
        fills=[
            _fill(
                start="0",
                side="A",
                size="2",
                fee="0.20",
            )
        ],
        funding=[
            _funding(
                position="-2",
                usdc="0.10",
            )
        ],
    )

    observed = await collect_profit_exit_economics(
        hl,
        account_address="0x" + "22" * 20,
        asset="BTC",
        history_start_ms=500,
        history_end_ms=2000,
        slippage_bps=50,
    )

    assert observed.complete is True

    # Closing a short is a BUY:
    # 90 * (1 + 50bps) = 90.45.
    assert observed.executable_exit_price == D("90.45")

    assert observed.economics is not None
    assert observed.economics.gross_price_pnl == D("19.10")
    assert observed.economics.eligible is True


@pytest.mark.asyncio
async def test_flat_position_fails_closed_without_loading_history():
    hl = FakeHL(
        snapshot=_snapshot(
            size="0",
            entry="100",
        )
    )

    observed = await collect_profit_exit_economics(
        hl,
        account_address="0x" + "33" * 20,
        asset="BTC",
        history_start_ms=500,
        history_end_ms=2000,
        slippage_bps=50,
    )

    assert observed.complete is False
    assert observed.eligible is False
    assert "flat" in observed.reason.lower()

    assert not any(
        call[0] in {"fills", "funding", "fees"}
        for call in hl.calls
    )


@pytest.mark.asyncio
async def test_missing_position_row_fails_closed():
    hl = FakeHL(
        snapshot=SimpleNamespace(
            perp_state={
                "assetPositions": []
            }
        )
    )

    observed = await collect_profit_exit_economics(
        hl,
        account_address="0x" + "44" * 20,
        asset="BTC",
        history_start_ms=500,
        history_end_ms=2000,
        slippage_bps=50,
    )

    assert observed.complete is False
    assert observed.eligible is False


@pytest.mark.asyncio
async def test_missing_entry_price_fails_closed():
    hl = FakeHL(
        snapshot=_snapshot(
            size="1",
            entry="",
        )
    )

    observed = await collect_profit_exit_economics(
        hl,
        account_address="0x" + "55" * 20,
        asset="BTC",
        history_start_ms=500,
        history_end_ms=2000,
        slippage_bps=50,
    )

    assert observed.complete is False
    assert observed.eligible is False
    assert "entry" in observed.reason.lower()


@pytest.mark.asyncio
async def test_missing_market_mid_fails_closed():
    hl = FakeHL(
        mids={}
    )

    observed = await collect_profit_exit_economics(
        hl,
        account_address="0x" + "66" * 20,
        asset="BTC",
        history_start_ms=500,
        history_end_ms=2000,
        slippage_bps=50,
    )

    assert observed.complete is False
    assert observed.eligible is False
    assert "market" in observed.reason.lower()


@pytest.mark.asyncio
async def test_other_assets_are_excluded_from_fill_and_funding_history():
    hl = FakeHL(
        fills=[
            _fill(
                coin="ETH",
                start="0",
                size="5",
                fee="99",
            ),
            _fill(),
        ],
        funding=[
            _funding(
                coin="ETH",
                position="5",
                usdc="-99",
            ),
            _funding(),
        ],
    )

    observed = await collect_profit_exit_economics(
        hl,
        account_address="0x" + "77" * 20,
        asset="BTC",
        history_start_ms=500,
        history_end_ms=2000,
        slippage_bps=50,
    )

    assert observed.complete is True
    assert observed.economics is not None
    assert observed.economics.residual_entry_fees == D("0.10")
    assert observed.economics.residual_funding == D("-0.20")


@pytest.mark.asyncio
async def test_incomplete_current_cycle_fails_closed():
    hl = FakeHL(
        snapshot=_snapshot(
            size="1.5",
            entry="100",
        ),
        fills=[
            _fill(
                start="1",
                side="B",
                size="0.5",
                fee="0.05",
            )
        ],
        funding=[],
    )

    observed = await collect_profit_exit_economics(
        hl,
        account_address="0x" + "88" * 20,
        asset="BTC",
        history_start_ms=500,
        history_end_ms=2000,
        slippage_bps=50,
    )

    assert observed.complete is False
    assert observed.eligible is False


@pytest.mark.asyncio
async def test_full_fill_page_is_treated_as_potentially_truncated():
    fills = [
        _fill(
            time=1000 + index,
            start="0" if index == 0 else "1",
            side="B",
            size="1",
            fee="0.01",
        )
        for index in range(2000)
    ]

    hl = FakeHL(
        fills=fills,
        funding=[],
    )

    observed = await collect_profit_exit_economics(
        hl,
        account_address="0x" + "99" * 20,
        asset="BTC",
        history_start_ms=500,
        history_end_ms=5000,
        slippage_bps=50,
    )

    assert observed.complete is False
    assert observed.eligible is False
    assert "trunc" in observed.reason.lower()


@pytest.mark.asyncio
async def test_read_failure_becomes_fail_closed_observation():
    hl = FakeHL()

    async def broken_fees(_account):
        raise RuntimeError("temporary Hyperliquid failure")

    hl.user_fees = broken_fees

    observed = await collect_profit_exit_economics(
        hl,
        account_address="0x" + "aa" * 20,
        asset="BTC",
        history_start_ms=500,
        history_end_ms=2000,
        slippage_bps=50,
    )

    assert observed.complete is False
    assert observed.eligible is False
    assert observed.economics is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "slippage_bps",
    [-1, 10001],
)
async def test_invalid_slippage_fails_closed(slippage_bps):
    hl = FakeHL()

    observed = await collect_profit_exit_economics(
        hl,
        account_address="0x" + "bb" * 20,
        asset="BTC",
        history_start_ms=500,
        history_end_ms=2000,
        slippage_bps=slippage_bps,
    )

    assert observed.complete is False
    assert observed.eligible is False


@pytest.mark.asyncio
async def test_invalid_history_window_fails_closed():
    hl = FakeHL()

    observed = await collect_profit_exit_economics(
        hl,
        account_address="0x" + "cc" * 20,
        asset="BTC",
        history_start_ms=2000,
        history_end_ms=1000,
        slippage_bps=50,
    )

    assert observed.complete is False
    assert observed.eligible is False
