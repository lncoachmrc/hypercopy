from decimal import Decimal
import uuid

import pytest

from app.services.ai_profit_exit import (
    ProfitExitIntentState,
    SourceCycle,
    profit_exit_reconcile_target,
)


def _cycle(name: str, *, position: str = "1") -> SourceCycle:
    event_id = uuid.uuid5(uuid.NAMESPACE_DNS, name)
    value = Decimal(position)
    return SourceCycle(
        open_event_id=event_id,
        source_cycle_id=f"mainnet:BTC:{event_id}",
        state_version=100,
        master_position=value,
        side="LONG" if value > 0 else "SHORT",
    )


def test_completed_exit_keeps_flat_follower_flat():
    cycle = _cycle("completed-flat")

    target = profit_exit_reconcile_target(
        desired_target=Decimal("0.75"),
        current_position=Decimal("0"),
        current_source_cycle=cycle,
        intent_state=ProfitExitIntentState.COMPLETED,
        intent_source_cycle_id=cycle.source_cycle_id,
    )

    assert target == Decimal("0")


@pytest.mark.parametrize(
    "state",
    [
        ProfitExitIntentState.PENDING,
        ProfitExitIntentState.PARTIAL,
        ProfitExitIntentState.AMBIGUOUS,
    ],
)
def test_inflight_or_partial_exit_freezes_real_residual(state):
    cycle = _cycle("residual")

    target = profit_exit_reconcile_target(
        desired_target=Decimal("1.50"),
        current_position=Decimal("0.20"),
        current_source_cycle=cycle,
        intent_state=state,
        intent_source_cycle_id=cycle.source_cycle_id,
    )

    assert target == Decimal("0.20")


def test_completed_exit_with_unexpected_residual_does_not_make_reconcile_close_it():
    cycle = _cycle("completed-residual")

    target = profit_exit_reconcile_target(
        desired_target=Decimal("1"),
        current_position=Decimal("0.10"),
        current_source_cycle=cycle,
        intent_state=ProfitExitIntentState.COMPLETED,
        intent_source_cycle_id=cycle.source_cycle_id,
    )

    assert target == Decimal("0.10")


@pytest.mark.parametrize(
    "desired_target",
    [Decimal("0.25"), Decimal("1"), Decimal("2.5")],
)
def test_master_scale_in_does_not_reopen_same_exited_cycle(desired_target):
    cycle = _cycle("master-scale-in")

    target = profit_exit_reconcile_target(
        desired_target=desired_target,
        current_position=Decimal("0"),
        current_source_cycle=cycle,
        intent_state=ProfitExitIntentState.COMPLETED,
        intent_source_cycle_id=cycle.source_cycle_id,
    )

    assert target == Decimal("0")


def test_short_same_cycle_is_also_frozen():
    cycle = _cycle("short-cycle", position="-1")

    target = profit_exit_reconcile_target(
        desired_target=Decimal("-0.8"),
        current_position=Decimal("-0.15"),
        current_source_cycle=cycle,
        intent_state=ProfitExitIntentState.PARTIAL,
        intent_source_cycle_id=cycle.source_cycle_id,
    )

    assert target == Decimal("-0.15")


def test_failed_pre_submit_exit_does_not_block_normal_reconcile():
    cycle = _cycle("failed")

    target = profit_exit_reconcile_target(
        desired_target=Decimal("0.75"),
        current_position=Decimal("0"),
        current_source_cycle=cycle,
        intent_state=ProfitExitIntentState.FAILED,
        intent_source_cycle_id=cycle.source_cycle_id,
    )

    assert target == Decimal("0.75")


def test_shadow_hold_or_abstain_does_not_block_normal_reconcile():
    cycle = _cycle("shadow")

    target = profit_exit_reconcile_target(
        desired_target=Decimal("0.75"),
        current_position=Decimal("0"),
        current_source_cycle=cycle,
        intent_state=None,
        intent_source_cycle_id=cycle.source_cycle_id,
    )

    assert target == Decimal("0.75")


def test_verified_new_source_cycle_reenables_master_target():
    old_cycle = _cycle("old-cycle")
    new_cycle = _cycle("new-cycle")

    target = profit_exit_reconcile_target(
        desired_target=Decimal("0.75"),
        current_position=Decimal("0"),
        current_source_cycle=new_cycle,
        intent_state=ProfitExitIntentState.COMPLETED,
        intent_source_cycle_id=old_cycle.source_cycle_id,
    )

    assert target == Decimal("0.75")


def test_unverified_new_cycle_does_not_unlock_existing_exit_memory():
    target = profit_exit_reconcile_target(
        desired_target=Decimal("0.75"),
        current_position=Decimal("0"),
        current_source_cycle=None,
        intent_state=ProfitExitIntentState.COMPLETED,
        intent_source_cycle_id="mainnet:BTC:old-cycle",
    )

    assert target == Decimal("0")


def test_unverified_cycle_preserves_partial_residual_without_second_close():
    target = profit_exit_reconcile_target(
        desired_target=Decimal("0.75"),
        current_position=Decimal("0.12"),
        current_source_cycle=None,
        intent_state=ProfitExitIntentState.PARTIAL,
        intent_source_cycle_id="mainnet:BTC:old-cycle",
    )

    assert target == Decimal("0.12")


@pytest.mark.parametrize(
    "state",
    [
        ProfitExitIntentState.PENDING,
        ProfitExitIntentState.PARTIAL,
        ProfitExitIntentState.COMPLETED,
        ProfitExitIntentState.AMBIGUOUS,
    ],
)
def test_master_or_safety_flat_target_always_wins(state):
    cycle = _cycle("master-flat")

    target = profit_exit_reconcile_target(
        desired_target=Decimal("0"),
        current_position=Decimal("0.25"),
        current_source_cycle=cycle,
        intent_state=state,
        intent_source_cycle_id=cycle.source_cycle_id,
    )

    assert target == Decimal("0")


def test_master_flat_target_also_wins_when_cycle_is_temporarily_unverified():
    target = profit_exit_reconcile_target(
        desired_target=Decimal("0"),
        current_position=Decimal("0.25"),
        current_source_cycle=None,
        intent_state=ProfitExitIntentState.PARTIAL,
        intent_source_cycle_id="mainnet:BTC:old-cycle",
    )

    assert target == Decimal("0")
