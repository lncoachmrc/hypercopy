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
    intent_state: ProfitExitIntentState | None,
    intent_source_cycle_id: str,
    current_source_cycle_id: str,
) -> bool:
    """
    Prevent normal master reconciliation from reopening exposure belonging to
    a source cycle already affected by an executable AI profit exit.

    Feature disablement must not erase durable exit memory. Shadow/HOLD/ABSTAIN
    decisions have no operational intent_state and therefore never suppress.
    A definitively failed pre-submit intent does not suppress either.
    """
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


from dataclasses import dataclass
import uuid

from app.models.entities import MasterEvent


@dataclass(frozen=True, slots=True)
class SourceCycle:
    open_event_id: uuid.UUID
    source_cycle_id: str
    state_version: int
    master_position: Decimal
    side: str


def _master_event_network(event: MasterEvent) -> str | None:
    raw = event.raw if isinstance(event.raw, dict) else {}
    value = raw.get("_hypercopy_network")
    if not value:
        return None
    return str(value).lower()


def _positive_event_causal_order(event: MasterEvent) -> int | None:
    raw = event.causal_order

    if raw is None or isinstance(raw, bool):
        return None

    try:
        value = int(raw)
        numeric = Decimal(str(raw))
    except (TypeError, ValueError, OverflowError):
        return None

    if value <= 0:
        return None

    if numeric != Decimal(value):
        return None

    return value


def resolve_current_source_cycle(
    events: list[MasterEvent],
    *,
    asset: str,
    master_network: str,
    current_master_position: Decimal,
) -> SourceCycle | None:
    """
    Resolve the active master position cycle using durable causal evidence.

    Fail closed when:
    - current master position is flat or invalid;
    - the opening/reversal event cannot be proven;
    - causal ordering is missing or ambiguous;
    - the position chain is discontinuous;
    - the latest event disagrees with the authoritative master snapshot.
    """
    if not current_master_position.is_finite():
        return None

    if current_master_position == 0:
        return None

    normalized_asset = str(asset).upper()
    normalized_network = str(master_network).lower()

    matching = [
        event
        for event in events
        if str(event.asset).upper() == normalized_asset
        and _master_event_network(event) == normalized_network
    ]

    if not matching:
        return None

    versioned: list[tuple[int, MasterEvent]] = []

    for event in matching:
        order = _positive_event_causal_order(event)
        if order is not None:
            versioned.append((order, event))

    if not versioned:
        return None

    versioned.sort(key=lambda item: item[0])

    orders = [order for order, _ in versioned]
    if len(orders) != len(set(orders)):
        return None

    latest_order, latest_event = versioned[-1]

    try:
        latest_position = Decimal(str(latest_event.position_after))
    except Exception:
        return None

    if not latest_position.is_finite():
        return None

    if latest_position != current_master_position:
        return None

    open_event: MasterEvent | None = None
    later_event: MasterEvent | None = None

    for _order, event in reversed(versioned):
        try:
            start = Decimal(str(event.start_position))
            after = Decimal(str(event.position_after))
        except Exception:
            return None

        if not start.is_finite() or not after.is_finite():
            return None

        transition = classify_source_cycle_transition(start, after)

        if later_event is not None:
            try:
                later_start = Decimal(str(later_event.start_position))
            except Exception:
                return None

            if after != later_start:
                return None

            # causal_order is authoritative; timestamps are only a sanity check.
            # Equal exchange timestamps are allowed.
            if event.event_ts > later_event.event_ts:
                return None

        if transition in {
            SourceCycleTransition.OPEN,
            SourceCycleTransition.REVERSE,
        }:
            open_event = event
            break

        if transition in {
            SourceCycleTransition.CLOSE,
            SourceCycleTransition.FLAT,
        }:
            return None

        later_event = event

    if open_event is None:
        return None

    # Any unversioned event that may belong to this active cycle makes the
    # history incomplete. Historical unversioned events strictly older than
    # the verified opening boundary do not invalidate the new cycle.
    for event in matching:
        if _positive_event_causal_order(event) is not None:
            continue

        try:
            if event.event_ts >= open_event.event_ts:
                return None
        except Exception:
            return None

    return SourceCycle(
        open_event_id=open_event.id,
        source_cycle_id=(
            f"{normalized_network}:{normalized_asset}:{open_event.id}"
        ),
        state_version=latest_order,
        master_position=current_master_position,
        side="LONG" if current_master_position > 0 else "SHORT",
    )
