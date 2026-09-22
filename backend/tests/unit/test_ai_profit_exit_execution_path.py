from __future__ import annotations

import inspect
from decimal import Decimal

import pytest

from app.engine.sizing import OrderIntent
from app.services import execution
from app.services.ai_profit_exit import (
    PROFIT_EXIT_ORIGIN,
    build_profit_exit_close_plan,
)
from app.services.strategy_intents import STRATEGY_ORIGINS


def test_profit_exit_origin_does_not_weaken_master_strategy_fence() -> None:
    assert PROFIT_EXIT_ORIGIN == "AI_PROFIT_EXIT"
    assert STRATEGY_ORIGINS == {"EVENT", "RECONCILE"}
    assert PROFIT_EXIT_ORIGIN not in STRATEGY_ORIGINS


@pytest.mark.parametrize(
    ("current", "expected_buy"),
    [
        (Decimal("1.23456"), False),
        (Decimal("-1.23456"), True),
    ],
)
def test_profit_exit_plan_is_full_residual_reduce_only_close(
    current: Decimal,
    expected_buy: bool,
) -> None:
    plan = build_profit_exit_close_plan(
        asset="btc",
        current_position=current,
        mark_price=Decimal("100"),
        sz_decimals=5,
    )

    assert plan.asset == "BTC"
    assert plan.intent is OrderIntent.CLOSE
    assert plan.target_size == 0
    assert plan.current_size == current
    assert plan.delta == -current
    assert plan.order_size == abs(current)
    assert plan.is_buy is expected_buy
    assert plan.reduce_only is True
    assert plan.secondary is None
    assert plan.actionable is True


def test_profit_exit_plan_never_accepts_flat_position() -> None:
    with pytest.raises(ValueError, match="non-zero"):
        build_profit_exit_close_plan(
            asset="BTC",
            current_position=Decimal(0),
            mark_price=Decimal("100"),
            sz_decimals=5,
        )


def test_execution_routes_profit_exit_before_master_derived_sizing() -> None:
    source = inspect.getsource(execution._process_job_locked)
    route = source.index("if job.origin == PROFIT_EXIT_ORIGIN")
    master_equity = source.index("if master_eq <= 0")
    sizing = source.index("sizing = plan(")

    assert route < master_equity < sizing


def test_profit_exit_reuses_normal_execute_leg_and_final_callback() -> None:
    source = inspect.getsource(execution._process_ai_profit_exit_locked)

    assert "_execute_leg(" in source
    assert "before_submit=_revalidate_profit_exit" in source
    assert "collect_profit_exit_economics(" in source
    assert "read_current_source_cycle(" in source
    assert "profit_exit_economically_admissible(" in source
    assert "RiskContext(" in source
    assert "evaluate(" in source
    assert "update_leverage(" not in source


def test_execute_leg_forwards_final_authorization_without_second_executor() -> None:
    source = inspect.getsource(execution._execute_leg)

    assert "Execution(" in source
    assert "state=ExecutionState.SUBMITTING" in source
    assert "deterministic_cloid(" in source
    assert "before_submit=before_submit" in source
