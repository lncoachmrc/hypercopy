from decimal import Decimal
from types import SimpleNamespace

from app.engine.states import assert_execution_transition
from app.models.entities import ExecutionState
from app.services.execution import _ambiguity_reduction_plan_safe
from app.services.reconcile import _safe_ambiguity_reduction


def test_unknown_can_move_to_quarantine_but_quarantine_is_terminal():
    assert_execution_transition(ExecutionState.UNKNOWN, ExecutionState.QUARANTINED)
    assert_execution_transition(ExecutionState.SUBMITTING, ExecutionState.QUARANTINED)


def test_ambiguity_allows_only_same_side_reduction_or_full_close():
    assert _safe_ambiguity_reduction(Decimal('2'), Decimal('1')) is True
    assert _safe_ambiguity_reduction(Decimal('-2'), Decimal('-1')) is True
    assert _safe_ambiguity_reduction(Decimal('2'), Decimal('0')) is True

    assert _safe_ambiguity_reduction(Decimal('2'), Decimal('3')) is False
    assert _safe_ambiguity_reduction(Decimal('2'), Decimal('-1')) is False
    assert _safe_ambiguity_reduction(Decimal('0'), Decimal('0')) is False


def test_execution_guard_rejects_reversal_or_non_reduce_only_plan():
    safe = SimpleNamespace(
        actionable=True,
        reduce_only=True,
        secondary=None,
        order_size=Decimal('1'),
    )
    reversal = SimpleNamespace(
        actionable=True,
        reduce_only=True,
        secondary=object(),
        order_size=Decimal('1'),
    )
    increase = SimpleNamespace(
        actionable=True,
        reduce_only=False,
        secondary=None,
        order_size=Decimal('1'),
    )

    assert _ambiguity_reduction_plan_safe(safe) is True
    assert _ambiguity_reduction_plan_safe(reversal) is False
    assert _ambiguity_reduction_plan_safe(increase) is False
