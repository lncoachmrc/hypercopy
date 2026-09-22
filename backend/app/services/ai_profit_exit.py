from __future__ import annotations

from decimal import Decimal
from enum import Enum


class ProfitExitAction(str, Enum):
    HOLD = "HOLD"
    CLOSE_PROFIT = "CLOSE_PROFIT"
    ABSTAIN = "ABSTAIN"


class ProfitExitIntentState(str, Enum):
    PENDING = "PENDING"
    PARTIAL = "PARTIAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    AMBIGUOUS = "AMBIGUOUS"


class SourceCycleTransition(str, Enum):
    FLAT = "FLAT"
    OPEN = "OPEN"
    CONTINUE = "CONTINUE"
    CLOSE = "CLOSE"
    REVERSE = "REVERSE"


def classify_source_cycle_transition(
    start_position: Decimal,
    position_after: Decimal,
) -> SourceCycleTransition:
    """
    Classify a master position transition without introducing time/cooldown
    heuristics.

    A source cycle begins when the master moves from flat to non-zero.
    Same-sign changes remain inside the same cycle.
    Flat closes the cycle.
    A sign reversal ends the previous cycle and begins a new one.
    """
    if not start_position.is_finite() or not position_after.is_finite():
        raise ValueError("source position must be finite")

    if start_position == 0 and position_after == 0:
        return SourceCycleTransition.FLAT

    if start_position == 0:
        return SourceCycleTransition.OPEN

    if position_after == 0:
        return SourceCycleTransition.CLOSE

    if (start_position > 0) == (position_after > 0):
        return SourceCycleTransition.CONTINUE

    return SourceCycleTransition.REVERSE


def profit_exit_economically_admissible(
    *,
    net_pnl: Decimal,
    pnl_complete: bool,
    position_fresh: bool,
) -> bool:
    """
    Deterministic economic gate for an AI CLOSE_PROFIT decision.

    The AI chooses whether the profitable position should be closed.
    This function does not decide timing and does not impose a TP,
    retracement, duration, or minimum-profit threshold.

    Execution is admissible only when authoritative accounting is complete,
    position data is fresh, and residual position net PnL is strictly > 0.
    """
    if not pnl_complete or not position_fresh:
        return False

    if not net_pnl.is_finite():
        return False

    return net_pnl > 0


def suppresses_same_cycle_retarget(
    *,
    intent_state: ProfitExitIntentState,
    intent_source_cycle_id: str,
    current_source_cycle_id: str,
    execution_enabled: bool,
) -> bool:
    """
    Prevent normal master reconciliation from reopening exposure belonging to
    a source cycle already affected by an executable AI profit exit.

    Shadow-only decisions never suppress the normal master target.
    A definitively failed pre-submit intent does not suppress it either.
    """
    if not execution_enabled:
        return False

    if not intent_source_cycle_id or not current_source_cycle_id:
        return False

    if intent_source_cycle_id != current_source_cycle_id:
        return False

    return intent_state in {
        ProfitExitIntentState.PENDING,
        ProfitExitIntentState.PARTIAL,
        ProfitExitIntentState.COMPLETED,
        ProfitExitIntentState.AMBIGUOUS,
    }
