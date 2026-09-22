from decimal import Decimal

import pytest

from app.services.ai_profit_exit_economics import (
    ProfitExitEconomicsInput,
    ProfitExitFill,
    ProfitExitFunding,
    evaluate_profit_exit_economics,
)


D = Decimal


def _fill(
    *,
    time_ms: int,
    start: str,
    side: str,
    size: str,
    fee: str,
) -> ProfitExitFill:
    return ProfitExitFill(
        time_ms=time_ms,
        start_position=D(start),
        side=side,
        size=D(size),
        fee=D(fee),
    )


def _funding(
    *,
    time_ms: int,
    position: str,
    usdc: str,
) -> ProfitExitFunding:
    return ProfitExitFunding(
        time_ms=time_ms,
        position_size=D(position),
        usdc=D(usdc),
    )


def test_simple_long_uses_residual_entry_fee_funding_and_exit_fee():
    result = evaluate_profit_exit_economics(
        ProfitExitEconomicsInput(
            current_position=D("1"),
            entry_price=D("100"),
            executable_exit_price=D("110"),
            taker_fee_rate=D("0.001"),
            fills=[
                _fill(
                    time_ms=1,
                    start="0",
                    side="B",
                    size="1",
                    fee="0.10",
                ),
            ],
            funding=[
                _funding(
                    time_ms=2,
                    position="1",
                    usdc="0.20",
                ),
            ],
        )
    )

    assert result.complete is True
    assert result.gross_price_pnl == D("10")
    assert result.residual_entry_fees == D("0.10")
    assert result.residual_funding == D("0.20")
    assert result.estimated_exit_fee == D("0.110")
    assert result.net_pnl == D("9.990")
    assert result.eligible is True


def test_simple_short_uses_adverse_buy_exit_price():
    result = evaluate_profit_exit_economics(
        ProfitExitEconomicsInput(
            current_position=D("-2"),
            entry_price=D("100"),
            executable_exit_price=D("90"),
            taker_fee_rate=D("0.001"),
            fills=[
                _fill(
                    time_ms=1,
                    start="0",
                    side="A",
                    size="2",
                    fee="0.20",
                ),
            ],
            funding=[
                _funding(
                    time_ms=2,
                    position="-2",
                    usdc="-0.10",
                ),
            ],
        )
    )

    assert result.complete is True
    assert result.gross_price_pnl == D("20")
    assert result.residual_entry_fees == D("0.20")
    assert result.residual_funding == D("-0.10")
    assert result.estimated_exit_fee == D("0.180")
    assert result.net_pnl == D("19.520")
    assert result.eligible is True


def test_scale_in_then_reduce_allocates_costs_only_to_current_residual():
    result = evaluate_profit_exit_economics(
        ProfitExitEconomicsInput(
            current_position=D("1"),
            entry_price=D("105"),
            executable_exit_price=D("120"),
            taker_fee_rate=D("0.001"),
            fills=[
                _fill(
                    time_ms=1,
                    start="0",
                    side="B",
                    size="1",
                    fee="0.10",
                ),
                _fill(
                    time_ms=2,
                    start="1",
                    side="B",
                    size="1",
                    fee="0.11",
                ),
                _fill(
                    time_ms=4,
                    start="2",
                    side="A",
                    size="1",
                    fee="0.12",
                ),
            ],
            funding=[
                _funding(
                    time_ms=3,
                    position="2",
                    usdc="0.20",
                ),
            ],
        )
    )

    # Before the reduction the entry-fee pool is 0.21 and funding pool 0.20.
    # Closing half the position removes half of both pools from the residual.
    assert result.complete is True
    assert result.residual_entry_fees == D("0.105")
    assert result.residual_funding == D("0.10")

    # Previously realized closing fee/profit must not be charged to or used to
    # subsidize the current residual position.
    assert result.gross_price_pnl == D("15")
    assert result.estimated_exit_fee == D("0.120")
    assert result.net_pnl == D("14.875")
    assert result.eligible is True


def test_reversal_starts_new_residual_cost_cycle():
    result = evaluate_profit_exit_economics(
        ProfitExitEconomicsInput(
            current_position=D("-0.5"),
            entry_price=D("95"),
            executable_exit_price=D("90"),
            taker_fee_rate=D("0.001"),
            fills=[
                _fill(
                    time_ms=1,
                    start="0",
                    side="B",
                    size="1",
                    fee="0.10",
                ),
                _fill(
                    time_ms=3,
                    start="1",
                    side="A",
                    size="1.5",
                    fee="0.15",
                ),
            ],
            funding=[
                _funding(
                    time_ms=2,
                    position="1",
                    usdc="-0.05",
                ),
            ],
        )
    )

    # The 1.5 sell closes the old +1 long and opens only 0.5 short.
    # Therefore only 1/3 of that fill's fee belongs to the new short cycle.
    assert result.complete is True
    assert result.residual_entry_fees == D("0.05")
    assert result.residual_funding == D("0")
    assert result.gross_price_pnl == D("2.5")
    assert result.estimated_exit_fee == D("0.045")
    assert result.net_pnl == D("2.405")
    assert result.eligible is True


def test_previous_profitable_cycle_cannot_mask_current_residual_loss():
    result = evaluate_profit_exit_economics(
        ProfitExitEconomicsInput(
            current_position=D("1"),
            entry_price=D("100"),
            executable_exit_price=D("99"),
            taker_fee_rate=D("0.001"),
            fills=[
                # Old completed profitable cycle.
                _fill(
                    time_ms=1,
                    start="0",
                    side="B",
                    size="1",
                    fee="0.05",
                ),
                _fill(
                    time_ms=2,
                    start="1",
                    side="A",
                    size="1",
                    fee="0.05",
                ),
                # Current cycle.
                _fill(
                    time_ms=3,
                    start="0",
                    side="B",
                    size="1",
                    fee="0.10",
                ),
            ],
            funding=[],
        )
    )

    assert result.complete is True
    assert result.gross_price_pnl == D("-1")
    assert result.net_pnl < 0
    assert result.eligible is False


@pytest.mark.parametrize(
    ("exit_price", "expected_positive"),
    [
        ("100.30", True),
        ("100.20", False),
        ("100.00", False),
    ],
)
def test_net_not_gross_profit_controls_eligibility(
    exit_price,
    expected_positive,
):
    result = evaluate_profit_exit_economics(
        ProfitExitEconomicsInput(
            current_position=D("1"),
            entry_price=D("100"),
            executable_exit_price=D(exit_price),
            taker_fee_rate=D("0.001"),
            fills=[
                _fill(
                    time_ms=1,
                    start="0",
                    side="B",
                    size="1",
                    fee="0.10",
                ),
            ],
            funding=[],
        )
    )

    assert result.eligible is expected_positive
    assert result.eligible is (result.net_pnl > 0)


def test_missing_beginning_of_current_position_cycle_fails_closed():
    result = evaluate_profit_exit_economics(
        ProfitExitEconomicsInput(
            current_position=D("1.5"),
            entry_price=D("100"),
            executable_exit_price=D("110"),
            taker_fee_rate=D("0.001"),
            fills=[
                # History starts while 1 BTC was already open.
                _fill(
                    time_ms=2,
                    start="1",
                    side="B",
                    size="0.5",
                    fee="0.05",
                ),
            ],
            funding=[],
        )
    )

    assert result.complete is False
    assert result.eligible is False
    assert result.net_pnl is None


def test_fill_history_must_reconstruct_authoritative_current_position():
    result = evaluate_profit_exit_economics(
        ProfitExitEconomicsInput(
            current_position=D("1"),
            entry_price=D("100"),
            executable_exit_price=D("110"),
            taker_fee_rate=D("0.001"),
            fills=[
                _fill(
                    time_ms=1,
                    start="0",
                    side="B",
                    size="2",
                    fee="0.20",
                ),
            ],
            funding=[],
        )
    )

    assert result.complete is False
    assert result.eligible is False


def test_funding_position_mismatch_fails_closed():
    result = evaluate_profit_exit_economics(
        ProfitExitEconomicsInput(
            current_position=D("1"),
            entry_price=D("100"),
            executable_exit_price=D("110"),
            taker_fee_rate=D("0.001"),
            fills=[
                _fill(
                    time_ms=1,
                    start="0",
                    side="B",
                    size="1",
                    fee="0.10",
                ),
            ],
            funding=[
                _funding(
                    time_ms=2,
                    position="2",
                    usdc="0.20",
                ),
            ],
        )
    )

    assert result.complete is False
    assert result.eligible is False


@pytest.mark.parametrize(
    ("position", "entry", "exit_price", "fee_rate"),
    [
        ("0", "100", "110", "0.001"),
        ("1", "0", "110", "0.001"),
        ("1", "100", "0", "0.001"),
        ("1", "100", "110", "-0.001"),
    ],
)
def test_invalid_or_non_actionable_inputs_fail_closed(
    position,
    entry,
    exit_price,
    fee_rate,
):
    result = evaluate_profit_exit_economics(
        ProfitExitEconomicsInput(
            current_position=D(position),
            entry_price=D(entry),
            executable_exit_price=D(exit_price),
            taker_fee_rate=D(fee_rate),
            fills=[],
            funding=[],
        )
    )

    assert result.complete is False
    assert result.eligible is False


def test_result_never_marks_zero_net_pnl_as_profitable():
    result = evaluate_profit_exit_economics(
        ProfitExitEconomicsInput(
            current_position=D("1"),
            entry_price=D("100"),
            executable_exit_price=D("100.2"),
            taker_fee_rate=D("0.001"),
            fills=[
                _fill(
                    time_ms=1,
                    start="0",
                    side="B",
                    size="1",
                    fee="0.0998",
                ),
            ],
            funding=[],
        )
    )

    assert result.net_pnl == D("0")
    assert result.eligible is False
