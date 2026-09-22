from __future__ import annotations

from decimal import Decimal
from enum import Enum

from app.core.config import settings
from app.engine.sizing import OrderIntent, SizingResult, round_size


PROFIT_EXIT_ORIGIN = "AI_PROFIT_EXIT"


class ProfitExitFeatureMode(str, Enum):
    OFF = "OFF"
    SHADOW = "SHADOW"
    ON = "ON"


def profit_exit_feature_mode() -> ProfitExitFeatureMode:
    return ProfitExitFeatureMode(settings.AI_PROFIT_EXIT_MODE)


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


def build_profit_exit_close_plan(
    *,
    asset: str,
    current_position: Decimal,
    mark_price: Decimal,
    sz_decimals: int,
) -> SizingResult:
    """Build the only order shape AI Profit Exit is allowed to submit.

    The AI may choose timing, but execution is always a full residual close of
    the currently verified follower side. The order can never open, increase or
    reverse exposure.
    """
    if not current_position.is_finite() or current_position == 0:
        raise ValueError("Profit-exit position must be finite and non-zero")
    if not mark_price.is_finite() or mark_price <= 0:
        raise ValueError("Profit-exit mark price must be finite and positive")
    if isinstance(sz_decimals, bool) or not isinstance(sz_decimals, int) or sz_decimals < 0:
        raise ValueError("Profit-exit size precision is invalid")

    order_size = round_size(abs(current_position), sz_decimals)
    if order_size <= 0:
        raise ValueError("Profit-exit residual rounds to zero")

    return SizingResult(
        asset=str(asset).upper(),
        intent=OrderIntent.CLOSE,
        target_size=Decimal(0),
        current_size=current_position,
        delta=-current_position,
        order_size=order_size,
        is_buy=current_position < 0,
        reduce_only=True,
        notional=order_size * mark_price,
        notes=["AI profit exit: full verified residual close"],
    )


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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import AIProfitExitDecision, MasterEvent


_PROFIT_EXIT_DECISION_NAMESPACE = uuid.UUID("2fa3a0ab-84d5-5d1a-84bf-241b7ae6a3b2")
_PROFIT_EXIT_JOB_NAMESPACE = uuid.UUID("67f4c698-1171-5c08-bc29-f61b421c89c7")


def profit_exit_decision_id(
    *,
    user_id: uuid.UUID,
    execution_epoch_id: uuid.UUID,
    execution_provider: str,
    execution_network: str,
    asset: str,
    source_cycle_id: str,
    state_version: int,
    follower_position: Decimal,
    evaluation_slot: int,
) -> uuid.UUID:
    """Deterministic semantic identity for one evaluated follower snapshot."""
    if evaluation_slot < 0 or state_version <= 0:
        raise ValueError("Profit-exit identity requires positive causal evidence")
    material = "|".join(
        (
            str(user_id),
            str(execution_epoch_id),
            str(execution_provider).lower(),
            str(execution_network).lower(),
            str(asset).upper(),
            str(source_cycle_id),
            str(state_version),
            format(follower_position, "f"),
            str(evaluation_slot),
        )
    )
    return uuid.uuid5(_PROFIT_EXIT_DECISION_NAMESPACE, material)


def profit_exit_job_id(decision_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(_PROFIT_EXIT_JOB_NAMESPACE, str(decision_id))


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



def profit_exit_reconcile_target(
    *,
    desired_target: Decimal,
    current_position: Decimal,
    current_source_cycle: SourceCycle | None,
    intent_state: ProfitExitIntentState | None,
    intent_source_cycle_id: str,
) -> Decimal:
    """
    Apply durable AI profit-exit memory to ordinary reconciliation.

    Reconciliation must not become a second profit-exit executor. While an
    operational AI exit belongs to the current cycle, preserve the follower's
    authoritative current position so reconcile neither reopens/increases it
    nor submits another close for a partial/ambiguous residual.

    If the current source cycle cannot yet be proven, existing operational
    memory remains conservative: do not re-enable master retargeting until a
    distinct new source cycle is verified.
    """
    # Ordinary/master/safety flattening always wins. AI memory may prevent
    # reopening/reintegration, but it must never block a deterministic close.
    if desired_target == 0:
        return desired_target

    operational = intent_state in {
        ProfitExitIntentState.PENDING,
        ProfitExitIntentState.PARTIAL,
        ProfitExitIntentState.COMPLETED,
        ProfitExitIntentState.AMBIGUOUS,
    }

    if not operational or not intent_source_cycle_id:
        return desired_target

    if current_source_cycle is None:
        return current_position

    if suppresses_same_cycle_retarget(
        intent_state=intent_state,
        intent_source_cycle_id=intent_source_cycle_id,
        current_source_cycle_id=current_source_cycle.source_cycle_id,
    ):
        return current_position

    return desired_target



def operational_profit_exit_memory_stmt(
    *,
    user_id: uuid.UUID,
    execution_epoch_id: uuid.UUID,
    execution_provider: str,
    execution_network: str,
    asset: str,
    source_cycle_id: str | None,
):
    """
    Build the durable anti-reopen memory lookup for one exact destination.

    Decision expiry is intentionally not part of this query: expiry controls
    whether an AI decision may still be submitted, not whether an already
    operational exit may be forgotten.

    When the current source cycle is proven, require the exact cycle id.
    When it is temporarily unprovable, retain conservative visibility of the
    latest operational exit memory inside the exact destination/asset scope
    until a distinct new cycle is verified.
    """
    operational_states = (
        ProfitExitIntentState.PENDING.value,
        ProfitExitIntentState.PARTIAL.value,
        ProfitExitIntentState.COMPLETED.value,
        ProfitExitIntentState.AMBIGUOUS.value,
    )

    stmt = select(AIProfitExitDecision).where(
        AIProfitExitDecision.user_id == user_id,
        AIProfitExitDecision.execution_epoch_id == execution_epoch_id,
        AIProfitExitDecision.execution_provider
        == str(execution_provider).lower(),
        AIProfitExitDecision.execution_network
        == str(execution_network).lower(),
        AIProfitExitDecision.asset == str(asset).upper(),
        AIProfitExitDecision.action == ProfitExitAction.CLOSE_PROFIT.value,
        AIProfitExitDecision.intent_state.in_(operational_states),
    )

    if source_cycle_id is not None:
        stmt = stmt.where(
            AIProfitExitDecision.source_cycle_id == source_cycle_id
        )

    return stmt.order_by(
        AIProfitExitDecision.decided_at.desc(),
        AIProfitExitDecision.created_at.desc(),
    ).limit(1)


async def read_operational_profit_exit_memory(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    execution_epoch_id: uuid.UUID,
    execution_provider: str,
    execution_network: str,
    asset: str,
    source_cycle_id: str | None,
) -> AIProfitExitDecision | None:
    stmt = operational_profit_exit_memory_stmt(
        user_id=user_id,
        execution_epoch_id=execution_epoch_id,
        execution_provider=execution_provider,
        execution_network=execution_network,
        asset=asset,
        source_cycle_id=source_cycle_id,
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def source_cycle_events_stmt(
    *,
    asset: str,
    snapshot_started_order: int,
):
    """
    Read only master events that existed before the authoritative master
    snapshot started.

    Events allocated at or after snapshot_started_order cannot be used to
    explain that snapshot retroactively.
    """
    return (
        select(MasterEvent)
        .where(
            MasterEvent.asset == str(asset).upper(),
            MasterEvent.causal_order.is_not(None),
            MasterEvent.causal_order < snapshot_started_order,
        )
        .order_by(
            MasterEvent.causal_order.asc(),
            MasterEvent.event_ts.asc(),
        )
    )


async def read_current_source_cycle(
    db: AsyncSession,
    *,
    asset: str,
    master_network: str,
    snapshot_started_order: int | None,
    current_master_position: Decimal,
) -> SourceCycle | None:
    """
    Resolve the source cycle visible to one specific master snapshot.

    Missing causal-boundary evidence deliberately returns None rather than
    guessing from timestamps or later master events.
    """
    if (
        snapshot_started_order is None
        or isinstance(snapshot_started_order, bool)
        or snapshot_started_order <= 0
    ):
        return None

    events = (
        await db.execute(
            source_cycle_events_stmt(
                asset=asset,
                snapshot_started_order=snapshot_started_order,
            )
        )
    ).scalars().all()

    return resolve_current_source_cycle(
        list(events),
        asset=asset,
        master_network=master_network,
        current_master_position=current_master_position,
    )


async def protected_reconcile_target(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    execution_epoch_id: uuid.UUID,
    execution_provider: str,
    execution_network: str,
    asset: str,
    master_network: str,
    snapshot_started_order: int | None,
    current_master_position: Decimal,
    current_position: Decimal,
    desired_target: Decimal,
) -> Decimal:
    """
    Apply durable profit-exit memory to a master-derived reconcile target.

    This function never creates a close decision. It only prevents ordinary
    reconciliation from reopening/reintegrating exposure already affected by
    an operational AI profit exit.

    Hyperliquid is the only enabled v1 provider for this feature.
    """
    # Master/safety flattening has unconditional precedence.
    if desired_target == 0:
        return desired_target

    # v1 is intentionally Hyperliquid-only.
    if str(execution_provider).lower() != "hyperliquid":
        return desired_target

    current_cycle = await read_current_source_cycle(
        db,
        asset=asset,
        master_network=master_network,
        snapshot_started_order=snapshot_started_order,
        current_master_position=current_master_position,
    )

    memory = await read_operational_profit_exit_memory(
        db,
        user_id=user_id,
        execution_epoch_id=execution_epoch_id,
        execution_provider=execution_provider,
        execution_network=execution_network,
        asset=asset,
        source_cycle_id=(
            current_cycle.source_cycle_id
            if current_cycle is not None
            else None
        ),
    )

    if memory is None:
        return desired_target

    raw_state = memory.intent_state
    try:
        intent_state = (
            raw_state
            if isinstance(raw_state, ProfitExitIntentState)
            else ProfitExitIntentState(str(raw_state))
        )
    except (TypeError, ValueError):
        return desired_target

    return profit_exit_reconcile_target(
        desired_target=desired_target,
        current_position=current_position,
        current_source_cycle=current_cycle,
        intent_state=intent_state,
        intent_source_cycle_id=str(memory.source_cycle_id or ""),
    )
