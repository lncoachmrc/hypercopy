from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.engine.sizing import OrderIntent, SizingResult
from app.services.shadow_positions import (
    ShadowPositionState,
    apply_shadow_plan,
    collect_shadow_profit_exit_economics,
)


D = Decimal


def _plan(
    *,
    intent: OrderIntent,
    target: str,
    current: str,
    order: str,
    is_buy: bool,
    reduce_only: bool,
    secondary: SizingResult | None = None,
) -> SizingResult:
    target_d = D(target)
    current_d = D(current)
    order_d = D(order)
    return SizingResult(
        asset="BTC",
        intent=intent,
        target_size=target_d,
        current_size=current_d,
        delta=target_d - current_d,
        order_size=order_d,
        is_buy=is_buy,
        reduce_only=reduce_only,
        notional=order_d * D("100"),
        secondary=secondary,
    )


def test_shadow_plan_tracks_open_scale_in_and_reduction() -> None:
    flat = ShadowPositionState(D("0"), D("0"), D("0"))
    opened = apply_shadow_plan(
        flat,
        _plan(
            intent=OrderIntent.OPEN,
            target="1",
            current="0",
            order="1",
            is_buy=True,
            reduce_only=False,
        ),
        mark_price=D("100"),
        slippage_bps=50,
        sz_decimals=4,
    )

    assert opened.state.size == D("1")
    assert opened.state.avg_entry_price == D("100.5")
    assert opened.state.residual_entry_notional == D("100.5")

    scaled = apply_shadow_plan(
        opened.state,
        _plan(
            intent=OrderIntent.OPEN,
            target="2",
            current="1",
            order="1",
            is_buy=True,
            reduce_only=False,
        ),
        mark_price=D("110"),
        slippage_bps=50,
        sz_decimals=4,
    )
    assert scaled.state.size == D("2")
    assert scaled.state.avg_entry_price == D("105.525")
    assert scaled.state.residual_entry_notional == D("211.05")

    reduced = apply_shadow_plan(
        scaled.state,
        _plan(
            intent=OrderIntent.REDUCE,
            target="1",
            current="2",
            order="1",
            is_buy=False,
            reduce_only=True,
        ),
        mark_price=D("120"),
        slippage_bps=50,
        sz_decimals=4,
    )
    assert reduced.state.size == D("1")
    assert reduced.state.avg_entry_price == D("105.525")
    assert reduced.state.residual_entry_notional == D("105.525")


def test_shadow_reversal_resets_entry_basis_to_new_side() -> None:
    state = ShadowPositionState(D("1"), D("100"), D("100"))
    secondary = _plan(
        intent=OrderIntent.OPEN,
        target="-0.5",
        current="0",
        order="0.5",
        is_buy=False,
        reduce_only=False,
    )
    reversal = _plan(
        intent=OrderIntent.REVERSE,
        target="-0.5",
        current="1",
        order="1",
        is_buy=False,
        reduce_only=True,
        secondary=secondary,
    )

    result = apply_shadow_plan(
        state,
        reversal,
        mark_price=D("100"),
        slippage_bps=50,
        sz_decimals=4,
    )

    assert result.state.size == D("-0.5")
    assert result.state.avg_entry_price == D("99.5")
    assert result.state.residual_entry_notional == D("49.75")
    assert len(result.fills) == 2


class FakeHL:
    async def mids(self, **_kwargs):
        return {"BTC": "110"}

    async def asset_spec(self, _asset):
        return SimpleNamespace(sz_decimals=4)

    async def user_fees(self, _account):
        return {"userCrossRate": "0.001"}


@pytest.mark.asyncio
async def test_shadow_profit_exit_economics_is_profitable_but_incomplete_by_design() -> None:
    position = SimpleNamespace(
        asset="BTC",
        size=D("1"),
        avg_entry_price=D("100"),
        residual_entry_notional=D("100"),
    )

    observed = await collect_shadow_profit_exit_economics(
        FakeHL(),
        account_address="0x" + "11" * 20,
        position=position,
        slippage_bps=50,
    )

    assert observed.economics_basis == "SHADOW_SIMULATION_NO_FUNDING"
    assert observed.funding_included is False
    assert observed.complete is False
    assert observed.eligible is True
    assert observed.executable_exit_price == D("109.45")
    assert observed.economics is not None
    assert observed.economics.complete is False
    assert observed.economics.gross_price_pnl == D("9.45")
    assert observed.economics.residual_entry_fees == D("0.100")
    assert observed.economics.estimated_exit_fee == D("0.10945")
    assert observed.economics.net_pnl == D("9.24055")


@pytest.mark.asyncio
async def test_shadow_profit_exit_does_not_call_profitable_when_price_regresses() -> None:
    class LosingHL(FakeHL):
        async def mids(self, **_kwargs):
            return {"BTC": "99"}

    position = SimpleNamespace(
        asset="BTC",
        size=D("1"),
        avg_entry_price=D("100"),
        residual_entry_notional=D("100"),
    )

    observed = await collect_shadow_profit_exit_economics(
        LosingHL(),
        account_address="0x" + "22" * 20,
        position=position,
        slippage_bps=50,
    )

    assert observed.eligible is False
    assert observed.economics is not None
    assert observed.economics.net_pnl < 0
