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
    return SourceCycle(
        open_event_id=event_id,
        source_cycle_id=f"mainnet:BTC:{event_id}",
        state_version=100,
        master_position=Decimal(position),
        side="LONG" if Decimal(position) > 0 else "SHORT",
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
def test_same_cycle_operational_exit_memory_forces_reconcile_target_to_zero(state):
    cycle = _cycle("same-cycle")

    target = profit_exit_reconcile_target(
        desired_target=Decimal("0.75"),
        current_source_cycle=cycle,
        intent_state=state,
        intent_source_cycle_id=cycle.source_cycle_id,
    )

    assert target == Decimal("0")


@pytest.mark.parametrize(
    "desired_target",
    [Decimal("0.25"), Decimal("1"), Decimal("2.5")],
)
def test_master_scale_in_does_not_reopen_same_exited_cycle(desired_target):
    cycle = _cycle("master-scale-in")

    target = profit_exit_reconcile_target(
        desired_target=desired_target,
        current_source_cycle=cycle,
        intent_state=ProfitExitIntentState.COMPLETED,
        intent_source_cycle_id=cycle.source_cycle_id,
    )

    assert target == Decimal("0")


def test_short_same_cycle_is_also_suppressed():
    cycle = _cycle("short-cycle", position="-1")

    target = profit_exit_reconcile_target(
        desired_target=Decimal("-0.8"),
        current_source_cycle=cycle,
        intent_state=ProfitExitIntentState.COMPLETED,
        intent_source_cycle_id=cycle.source_cycle_id,
    )

    assert target == Decimal("0")


def test_failed_pre_submit_exit_does_not_block_normal_reconcile():
    cycle = _cycle("failed")

    target = profit_exit_reconcile_target(
        desired_target=Decimal("0.75"),
        current_source_cycle=cycle,
        intent_state=ProfitExitIntentState.FAILED,
        intent_source_cycle_id=cycle.source_cycle_id,
    )

    assert target == Decimal("0.75")


def test_no_operational_memory_does_not_block_normal_reconcile():
    cycle = _cycle("shadow")

    target = profit_exit_reconcile_target(
        desired_target=Decimal("0.75"),
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
        current_source_cycle=new_cycle,
        intent_state=ProfitExitIntentState.COMPLETED,
        intent_source_cycle_id=old_cycle.source_cycle_id,
    )

    assert target == Decimal("0.75")


def test_unverified_current_cycle_fails_closed_for_ai_suppression_only():
    target = profit_exit_reconcile_target(
        desired_target=Decimal("0.75"),
        current_source_cycle=None,
        intent_state=ProfitExitIntentState.COMPLETED,
        intent_source_cycle_id="mainnet:BTC:unknown",
    )

    # Lack of source-cycle proof must not let stale AI memory override
    # the ordinary deterministic reconcile path.
    assert target == Decimal("0.75")
