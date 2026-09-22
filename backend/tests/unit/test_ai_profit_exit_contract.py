from decimal import Decimal

import pytest

from app.services.ai_profit_exit import (
    ProfitExitAction,
    ProfitExitIntentState,
    SourceCycleTransition,
    classify_source_cycle_transition,
    profit_exit_economically_admissible,
    suppresses_same_cycle_retarget,
)


def test_profit_exit_action_contract_is_exact_and_closed():
    assert {action.value for action in ProfitExitAction} == {
        "HOLD",
        "CLOSE_PROFIT",
        "ABSTAIN",
    }


def test_profit_exit_persistence_states_are_explicit():
    assert {state.value for state in ProfitExitIntentState} == {
        "PENDING",
        "PARTIAL",
        "COMPLETED",
        "FAILED",
        "AMBIGUOUS",
    }


@pytest.mark.parametrize(
    ("start_position", "position_after", "expected"),
    [
        ("0", "1", SourceCycleTransition.OPEN),
        ("0", "-1", SourceCycleTransition.OPEN),
        ("1", "2", SourceCycleTransition.CONTINUE),
        ("2", "0.5", SourceCycleTransition.CONTINUE),
        ("-1", "-2", SourceCycleTransition.CONTINUE),
        ("-2", "-0.5", SourceCycleTransition.CONTINUE),
        ("1", "0", SourceCycleTransition.CLOSE),
        ("-1", "0", SourceCycleTransition.CLOSE),
        ("1", "-1", SourceCycleTransition.REVERSE),
        ("-1", "1", SourceCycleTransition.REVERSE),
        ("0", "0", SourceCycleTransition.FLAT),
    ],
)
def test_source_cycle_transition_is_derived_from_master_position_transition(
    start_position,
    position_after,
    expected,
):
    assert (
        classify_source_cycle_transition(
            Decimal(start_position),
            Decimal(position_after),
        )
        is expected
    )


def test_any_strictly_positive_net_profit_is_economically_admissible():
    assert profit_exit_economically_admissible(
        net_pnl=Decimal("0.00000001"),
        pnl_complete=True,
        position_fresh=True,
    )


@pytest.mark.parametrize("net_pnl", ["0", "-0.00000001", "-10"])
def test_zero_or_negative_net_profit_is_never_admissible(net_pnl):
    assert not profit_exit_economically_admissible(
        net_pnl=Decimal(net_pnl),
        pnl_complete=True,
        position_fresh=True,
    )


def test_incomplete_cost_accounting_forces_abstention():
    assert not profit_exit_economically_admissible(
        net_pnl=Decimal("100"),
        pnl_complete=False,
        position_fresh=True,
    )


def test_stale_position_forces_abstention():
    assert not profit_exit_economically_admissible(
        net_pnl=Decimal("100"),
        pnl_complete=True,
        position_fresh=False,
    )


@pytest.mark.parametrize(
    "state",
    [
        ProfitExitIntentState.PENDING,
        ProfitExitIntentState.PARTIAL,
        ProfitExitIntentState.COMPLETED,
        ProfitExitIntentState.AMBIGUOUS,
    ],
)
def test_live_or_effectful_exit_memory_suppresses_same_cycle_retarget(state):
    assert suppresses_same_cycle_retarget(
        intent_state=state,
        intent_source_cycle_id="btc:cycle-123",
        current_source_cycle_id="btc:cycle-123",
    )


def test_failed_pre_submit_intent_does_not_suppress_retarget():
    assert not suppresses_same_cycle_retarget(
        intent_state=ProfitExitIntentState.FAILED,
        intent_source_cycle_id="btc:cycle-123",
        current_source_cycle_id="btc:cycle-123",
    )


def test_new_verified_source_cycle_reenables_master_target():
    assert not suppresses_same_cycle_retarget(
        intent_state=ProfitExitIntentState.COMPLETED,
        intent_source_cycle_id="btc:cycle-123",
        current_source_cycle_id="btc:cycle-456",
    )


def test_non_operational_shadow_hold_or_abstain_never_suppresses_master_target():
    assert not suppresses_same_cycle_retarget(
        intent_state=None,
        intent_source_cycle_id="btc:cycle-123",
        current_source_cycle_id="btc:cycle-123",
    )


def test_same_cycle_completed_memory_does_not_depend_on_current_feature_mode():
    import inspect

    parameters = inspect.signature(suppresses_same_cycle_retarget).parameters
    assert "execution_enabled" not in parameters

    assert suppresses_same_cycle_retarget(
        intent_state=ProfitExitIntentState.COMPLETED,
        intent_source_cycle_id="btc:cycle-123",
        current_source_cycle_id="btc:cycle-123",
    )
