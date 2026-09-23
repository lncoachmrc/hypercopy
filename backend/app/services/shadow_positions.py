from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.ratelimit import Priority
from app.engine.sizing import SizingResult, round_price, round_size
from app.models.entities import ShadowPositionLedger
from app.services.ai_profit_exit_collector import ProfitExitObservation
from app.services.ai_profit_exit_economics import ProfitExitEconomicsResult


@dataclass(frozen=True, slots=True)
class ShadowPositionState:
    size: Decimal
    avg_entry_price: Decimal
    residual_entry_notional: Decimal


@dataclass(frozen=True, slots=True)
class ShadowFillEvidence:
    is_buy: bool
    size: Decimal
    price: Decimal
    reduce_only: bool


@dataclass(frozen=True, slots=True)
class ShadowPlanResult:
    state: ShadowPositionState
    fills: tuple[ShadowFillEvidence, ...]


def _finite(value: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def _shadow_fill_price(
    mark_price: Decimal,
    *,
    is_buy: bool,
    slippage_bps: int,
    sz_decimals: int,
) -> Decimal:
    if not _finite(mark_price) or mark_price <= 0:
        raise ValueError("Shadow mark price must be positive")
    if (
        isinstance(slippage_bps, bool)
        or not isinstance(slippage_bps, int)
        or slippage_bps < 0
        or slippage_bps >= 10_000
    ):
        raise ValueError("Shadow slippage must be between 0 and 9999 bps")
    slip = Decimal(slippage_bps) / Decimal(10_000)
    aggressive = mark_price * (
        Decimal(1) + slip if is_buy else Decimal(1) - slip
    )
    return round_price(aggressive, sz_decimals)


def _apply_shadow_fill(
    state: ShadowPositionState,
    *,
    is_buy: bool,
    size: Decimal,
    price: Decimal,
    reduce_only: bool,
) -> ShadowPositionState:
    if not _finite(size) or size <= 0:
        raise ValueError("Shadow fill size must be positive")
    if not _finite(price) or price <= 0:
        raise ValueError("Shadow fill price must be positive")

    previous = state.size
    delta = size if is_buy else -size

    if reduce_only:
        if previous == 0:
            raise ValueError("Shadow reduce-only fill cannot start from flat")
        if previous * delta >= 0:
            raise ValueError("Shadow reduce-only fill must oppose the current side")
        if size > abs(previous):
            raise ValueError("Shadow reduce-only fill cannot cross through flat")

    updated = previous + delta
    avg = state.avg_entry_price
    residual = state.residual_entry_notional

    if previous == 0:
        avg = price
        residual = abs(updated) * price

    elif updated == 0:
        avg = Decimal(0)
        residual = Decimal(0)

    elif previous * updated > 0:
        previous_abs = abs(previous)
        updated_abs = abs(updated)
        if updated_abs > previous_abs:
            added = updated_abs - previous_abs
            total_cost = previous_abs * avg + added * price
            avg = total_cost / updated_abs
            residual += added * price
        elif updated_abs < previous_abs:
            ratio = updated_abs / previous_abs
            residual *= ratio
        else:
            raise ValueError("Shadow fill produced no position-size change")

    else:
        # This path should normally be represented by a close leg plus a
        # secondary opening leg. Keep it correct if a future provider emits a
        # single cross-zero simulation.
        opening_size = abs(updated)
        avg = price
        residual = opening_size * price

    return ShadowPositionState(
        size=updated,
        avg_entry_price=avg,
        residual_entry_notional=residual,
    )


def apply_shadow_plan(
    state: ShadowPositionState,
    plan: SizingResult,
    *,
    mark_price: Decimal,
    slippage_bps: int,
    sz_decimals: int,
) -> ShadowPlanResult:
    current = state
    fills: list[ShadowFillEvidence] = []

    legs = [plan]
    if plan.secondary is not None:
        legs.append(plan.secondary)

    for leg in legs:
        if not leg.actionable:
            continue
        fill_size = round_size(leg.order_size, sz_decimals)
        if fill_size <= 0:
            continue
        fill_price = _shadow_fill_price(
            mark_price,
            is_buy=leg.is_buy,
            slippage_bps=slippage_bps,
            sz_decimals=sz_decimals,
        )
        current = _apply_shadow_fill(
            current,
            is_buy=leg.is_buy,
            size=fill_size,
            price=fill_price,
            reduce_only=leg.reduce_only,
        )
        fills.append(
            ShadowFillEvidence(
                is_buy=leg.is_buy,
                size=fill_size,
                price=fill_price,
                reduce_only=leg.reduce_only,
            )
        )

    return ShadowPlanResult(state=current, fills=tuple(fills))


async def get_or_create_shadow_position(
    db: AsyncSession,
    *,
    user_id,
    execution_epoch_id,
    execution_provider: str,
    execution_network: str,
    shadow_started_at: datetime,
    asset: str,
) -> ShadowPositionLedger:
    row = (
        await db.execute(
            select(ShadowPositionLedger).where(
                ShadowPositionLedger.user_id == user_id,
                ShadowPositionLedger.shadow_started_at == shadow_started_at,
                ShadowPositionLedger.asset == str(asset).upper(),
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        if (
            row.execution_epoch_id != execution_epoch_id
            or row.execution_provider != execution_provider
            or row.execution_network != execution_network
        ):
            raise RuntimeError(
                "Shadow position belongs to a stale execution destination"
            )
        return row

    row = ShadowPositionLedger(
        user_id=user_id,
        execution_epoch_id=execution_epoch_id,
        execution_provider=execution_provider,
        execution_network=execution_network,
        shadow_started_at=shadow_started_at,
        asset=str(asset).upper(),
        size=Decimal(0),
        avg_entry_price=Decimal(0),
        residual_entry_notional=Decimal(0),
        mark_price=Decimal(0),
    )
    db.add(row)
    await db.flush()
    return row


async def current_shadow_positions(
    db: AsyncSession,
    *,
    user_id,
    shadow_started_at: datetime,
) -> list[ShadowPositionLedger]:
    return (
        await db.execute(
            select(ShadowPositionLedger)
            .where(
                ShadowPositionLedger.user_id == user_id,
                ShadowPositionLedger.shadow_started_at == shadow_started_at,
            )
            .order_by(ShadowPositionLedger.asset)
        )
    ).scalars().all()


def persist_shadow_plan_result(
    row: ShadowPositionLedger,
    result: ShadowPlanResult,
    *,
    mark_price: Decimal,
    simulated_at: datetime | None = None,
) -> None:
    now = simulated_at or datetime.now(UTC)
    before = Decimal(str(row.size))
    row.size = result.state.size
    row.avg_entry_price = result.state.avg_entry_price
    row.residual_entry_notional = result.state.residual_entry_notional
    row.mark_price = mark_price
    row.last_simulated_at = now

    if before == 0 and result.state.size != 0:
        row.opened_at = now
    elif result.state.size == 0:
        row.opened_at = None
    elif before * result.state.size < 0:
        row.opened_at = now


def _decimal(value) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


async def collect_shadow_profit_exit_economics(
    hl,
    *,
    account_address: str,
    position: ShadowPositionLedger,
    slippage_bps: int,
) -> ProfitExitObservation:
    asset = str(position.asset).upper()
    current = _decimal(position.size)
    entry = _decimal(position.avg_entry_price)
    residual_entry_notional = _decimal(position.residual_entry_notional)

    if current is None or current == 0:
        return ProfitExitObservation(
            asset=asset,
            complete=False,
            eligible=False,
            current_position=current,
            entry_price=entry,
            mark_price=None,
            executable_exit_price=None,
            taker_fee_rate=None,
            economics=None,
            reason="Shadow position is flat or invalid",
            economics_basis="SHADOW_SIMULATION_NO_FUNDING",
            funding_included=False,
        )
    if entry is None or entry <= 0 or residual_entry_notional is None or residual_entry_notional < 0:
        return ProfitExitObservation(
            asset=asset,
            complete=False,
            eligible=False,
            current_position=current,
            entry_price=entry,
            mark_price=None,
            executable_exit_price=None,
            taker_fee_rate=None,
            economics=None,
            reason="Shadow entry basis is incomplete",
            economics_basis="SHADOW_SIMULATION_NO_FUNDING",
            funding_included=False,
        )

    try:
        mids = await hl.mids(priority=Priority.RECONCILE)
        spec = await hl.asset_spec(asset)
        raw_fees = await hl.user_fees(account_address)
    except Exception as exc:
        return ProfitExitObservation(
            asset=asset,
            complete=False,
            eligible=False,
            current_position=current,
            entry_price=entry,
            mark_price=None,
            executable_exit_price=None,
            taker_fee_rate=None,
            economics=None,
            reason=f"Shadow market economics unavailable: {type(exc).__name__}",
            economics_basis="SHADOW_SIMULATION_NO_FUNDING",
            funding_included=False,
        )

    mark = _decimal(mids.get(asset)) if isinstance(mids, dict) else None
    fee_rate = (
        _decimal(raw_fees.get("userCrossRate"))
        if isinstance(raw_fees, dict)
        else None
    )
    if mark is None or mark <= 0 or fee_rate is None or fee_rate < 0:
        return ProfitExitObservation(
            asset=asset,
            complete=False,
            eligible=False,
            current_position=current,
            entry_price=entry,
            mark_price=mark,
            executable_exit_price=None,
            taker_fee_rate=fee_rate,
            economics=None,
            reason="Shadow market price or taker fee is unavailable",
            economics_basis="SHADOW_SIMULATION_NO_FUNDING",
            funding_included=False,
        )

    exit_price = _shadow_fill_price(
        mark,
        is_buy=current < 0,
        slippage_bps=slippage_bps,
        sz_decimals=int(spec.sz_decimals),
    )
    gross = (
        (exit_price - entry) * current
        if current > 0
        else (entry - exit_price) * abs(current)
    )
    estimated_entry_fee = residual_entry_notional * fee_rate
    estimated_exit_fee = abs(current) * exit_price * fee_rate
    net = gross - estimated_entry_fee - estimated_exit_fee

    economics = ProfitExitEconomicsResult(
        # Funding is deliberately not invented for a virtual position. The
        # simulated result is useful for SHADOW comparison but is never eligible
        # to become an operational order without the live revalidation path.
        complete=False,
        eligible=net > 0,
        gross_price_pnl=gross,
        residual_entry_fees=estimated_entry_fee,
        residual_funding=Decimal(0),
        estimated_exit_fee=estimated_exit_fee,
        net_pnl=net,
        reason=(
            "Shadow simulation excludes funding; PnL is price-and-fee adjusted"
        ),
    )
    return ProfitExitObservation(
        asset=asset,
        complete=False,
        eligible=net > 0,
        current_position=current,
        entry_price=entry,
        mark_price=mark,
        executable_exit_price=exit_price,
        taker_fee_rate=fee_rate,
        economics=economics,
        reason=economics.reason or "",
        economics_basis="SHADOW_SIMULATION_NO_FUNDING",
        funding_included=False,
    )
