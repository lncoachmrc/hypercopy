from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.risex_http import RISExReadOnlyHTTPTransport
from app.db.position_ledger_lock import position_ledger_lock
from app.models.entities import (
    Execution,
    ExecutionEpoch,
    ExecutionState,
    PositionLedger,
)
from app.services.risex_copy_execution import (
    RISExSubmissionOutcome,
    settle_risex_execution_under_accounting_lock,
)
from app.services.risex_risk_planning import (
    parse_risex_portfolio_details,
    parse_risex_risk_market,
)


_RISEX_TESTNET_API_URL = "https://api.testnet.rise.trade"
_ACTIVE_EXECUTION_STATES = (ExecutionState.SUBMITTING, ExecutionState.UNKNOWN)


@dataclass(frozen=True, slots=True)
class RISExLedgerBaseline:
    ledger_id: uuid.UUID | None
    size: Decimal | None
    target_size: Decimal | None
    mark_price: Decimal | None
    managed: bool | None
    last_execution_id: uuid.UUID | None
    exchange_verified_at: datetime | None


@dataclass(frozen=True, slots=True)
class RISExProviderTruthSnapshot:
    market_id: int
    position_size: Decimal
    mark_price: Decimal
    verified_at: datetime


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _terminal_outcome(execution: Execution) -> RISExSubmissionOutcome:
    response = dict(execution.response or {})
    case_a = response.get("risex_case_a")
    if not isinstance(case_a, Mapping):
        raise RuntimeError("RISEx terminal evidence is unavailable")
    raw = case_a.get("terminal_outcome")
    if not isinstance(raw, Mapping) or raw.get("definitive") is not True:
        raise RuntimeError("RISEx terminal evidence is unavailable")

    try:
        state = ExecutionState(str(raw.get("execution_state") or ""))
    except ValueError as exc:
        raise RuntimeError("RISEx terminal evidence state is invalid") from exc
    if state not in {
        ExecutionState.FILLED,
        ExecutionState.REJECTED,
        ExecutionState.CANCELED,
    }:
        raise RuntimeError("RISEx terminal evidence is not a terminal case-A outcome")

    filled = _decimal_or_none(raw.get("filled_quantity"))
    if state == ExecutionState.FILLED and (filled is None or filled <= 0):
        raise RuntimeError("RISEx FILLED terminal evidence lacks a positive fill")
    if state == ExecutionState.CANCELED and filled is None:
        filled = Decimal(0)

    provider_order_id = raw.get("provider_order_id")
    return RISExSubmissionOutcome(
        definitive=True,
        execution_state=state,
        reservation_active=False,
        provider_order_id=(
            str(provider_order_id) if provider_order_id not in (None, "") else None
        ),
        filled_quantity=filled,
        reason=(str(raw.get("reason")) if raw.get("reason") not in (None, "") else None),
    )


def _baseline_from_ledger(ledger: PositionLedger | None) -> RISExLedgerBaseline:
    if ledger is None:
        return RISExLedgerBaseline(
            ledger_id=None,
            size=None,
            target_size=None,
            mark_price=None,
            managed=None,
            last_execution_id=None,
            exchange_verified_at=None,
        )
    return RISExLedgerBaseline(
        ledger_id=ledger.id,
        size=ledger.size,
        target_size=ledger.target_size,
        mark_price=ledger.mark_price,
        managed=ledger.managed,
        last_execution_id=ledger.last_execution_id,
        exchange_verified_at=ledger.exchange_verified_at,
    )


async def _read_ledger(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    asset: str,
    for_update: bool,
) -> PositionLedger | None:
    query = (
        select(PositionLedger)
        .where(
            PositionLedger.user_id == user_id,
            PositionLedger.asset == asset,
        )
        .execution_options(populate_existing=True)
    )
    if for_update:
        query = query.with_for_update()
    return (await db.execute(query)).scalar_one_or_none()


async def read_risex_provider_truth_snapshot(
    execution: Execution,
    *,
    account_address: str,
) -> RISExProviderTruthSnapshot:
    """Read absolute RISEx follower truth without holding a PostgreSQL lock."""

    async with RISExReadOnlyHTTPTransport(base_url=_RISEX_TESTNET_API_URL) as api:
        markets_payload = await api.get_json("/v1/markets")
        portfolio_payload = await api.get_json(
            "/v1/portfolio/details",
            params={"account": account_address},
        )

    market = parse_risex_risk_market(markets_payload, symbol=execution.asset)
    portfolio = parse_risex_portfolio_details(portfolio_payload)
    matches = [
        position
        for position in portfolio.positions
        if position.market_id == market.market_id
    ]
    if len(matches) > 1:
        raise RuntimeError("RISEx provider truth contains duplicate market positions")

    if matches:
        position = matches[0]
        size = position.size
        mark_price = position.mark_price
    else:
        size = Decimal(0)
        mark_price = market.mark_price

    if mark_price is None or mark_price <= 0:
        raise RuntimeError("RISEx provider truth mark price is unavailable")

    return RISExProviderTruthSnapshot(
        market_id=market.market_id,
        position_size=size,
        mark_price=mark_price,
        verified_at=datetime.now(UTC),
    )


def _snapshot_from_result(result: object) -> RISExProviderTruthSnapshot:
    if isinstance(result, RISExProviderTruthSnapshot):
        return result
    if not isinstance(result, Mapping):
        raise RuntimeError("RISEx provider truth snapshot is malformed")
    market_id = result.get("market_id")
    position_size = _decimal_or_none(result.get("position_size"))
    mark_price = _decimal_or_none(result.get("mark_price"))
    verified_at = result.get("verified_at")
    if (
        type(market_id) is not int
        or market_id <= 0
        or position_size is None
        or mark_price is None
        or mark_price <= 0
        or not isinstance(verified_at, datetime)
    ):
        raise RuntimeError("RISEx provider truth snapshot is malformed")
    return RISExProviderTruthSnapshot(
        market_id=market_id,
        position_size=position_size,
        mark_price=mark_price,
        verified_at=verified_at,
    )


async def _apply_snapshot_under_settlement_lock(
    db: AsyncSession,
    execution: Execution,
    snapshot: RISExProviderTruthSnapshot,
) -> Mapping[str, Any]:
    ledger = await _read_ledger(
        db,
        user_id=execution.user_id,
        asset=execution.asset,
        for_update=True,
    )
    if ledger is None:
        ledger = PositionLedger(
            user_id=execution.user_id,
            asset=execution.asset,
            size=snapshot.position_size,
            target_size=snapshot.position_size,
            mark_price=snapshot.mark_price,
            managed=True,
            last_execution_id=execution.id,
            exchange_verified_at=snapshot.verified_at,
        )
        db.add(ledger)
    else:
        ledger.size = snapshot.position_size
        ledger.mark_price = snapshot.mark_price
        ledger.managed = True
        ledger.last_execution_id = execution.id
        ledger.exchange_verified_at = snapshot.verified_at

    response = dict(execution.response or {})
    response["risex_provider_truth"] = {
        "source": ["/v1/markets", "/v1/portfolio/details"],
        "market_id": snapshot.market_id,
        "position_size": str(snapshot.position_size),
        "mark_price": str(snapshot.mark_price),
        "verified_at": snapshot.verified_at.isoformat(),
    }
    execution.response = response
    await db.flush()
    return {
        "provider_truth_persisted": True,
        "position_size": str(snapshot.position_size),
        "verified_at": snapshot.verified_at.isoformat(),
    }


async def persist_risex_provider_truth(
    db: AsyncSession,
    execution: Execution,
) -> Mapping[str, Any]:
    """Finish a durable terminal RISEx outcome without DB locks during network I/O."""

    outcome = _terminal_outcome(execution)
    if execution.execution_epoch_id is None:
        raise RuntimeError("RISEx terminal settlement lost execution epoch binding")

    epoch = await db.get(ExecutionEpoch, execution.execution_epoch_id)
    if (
        epoch is None
        or epoch.user_id != execution.user_id
        or epoch.provider != "risex"
        or epoch.network != "testnet"
        or not epoch.account_address
    ):
        raise RuntimeError("RISEx terminal settlement execution epoch is invalid")

    ledger = await _read_ledger(
        db,
        user_id=execution.user_id,
        asset=execution.asset,
        for_update=False,
    )
    baseline = _baseline_from_ledger(ledger)
    execution_id = execution.id
    user_id = execution.user_id
    asset = execution.asset
    account_address = epoch.account_address

    # ADR-0004 B3: close the DB transaction before any RISEx network read.
    await db.commit()

    snapshot = _snapshot_from_result(
        await read_risex_provider_truth_snapshot(
            execution,
            account_address=account_address,
        )
    )

    async with position_ledger_lock(user_id):
        current_execution = await db.get(Execution, execution_id)
        if current_execution is None:
            await db.rollback()
            raise RuntimeError("RISEx terminal settlement execution disappeared")
        if current_execution.state not in _ACTIVE_EXECUTION_STATES:
            await db.rollback()
            return {
                "provider_truth_persisted": True,
                "provider_truth_settled": current_execution.state in {
                    ExecutionState.FILLED,
                    ExecutionState.REJECTED,
                    ExecutionState.CANCELED,
                },
                "reason": "execution_already_terminal",
            }

        current_ledger = await _read_ledger(
            db,
            user_id=user_id,
            asset=asset,
            for_update=True,
        )
        if _baseline_from_ledger(current_ledger) != baseline:
            await db.rollback()
            return {
                "provider_truth_persisted": False,
                "provider_truth_settled": False,
                "reason": "ledger_baseline_changed",
            }

        other_unresolved = (
            await db.execute(
                select(Execution.id)
                .where(
                    Execution.user_id == user_id,
                    Execution.asset == asset,
                    Execution.id != execution_id,
                    Execution.state.in_(_ACTIVE_EXECUTION_STATES),
                )
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if other_unresolved is not None:
            await db.rollback()
            return {
                "provider_truth_persisted": False,
                "provider_truth_settled": False,
                "reason": "other_unresolved_execution_same_asset",
            }

        async def persist_snapshot(
            settlement_db: AsyncSession,
            locked_execution: Execution,
        ) -> Mapping[str, Any]:
            return await _apply_snapshot_under_settlement_lock(
                settlement_db,
                locked_execution,
                snapshot,
            )

        if outcome.execution_state == ExecutionState.REJECTED:
            # The shared settlement helper preserves its historical no-refresh
            # contract for direct callers. Case A is stricter: every definitive
            # provider outcome converges the absolute follower ledger before the
            # reservation is released and the Execution becomes terminal.
            await persist_snapshot(db, current_execution)
            settled = await settle_risex_execution_under_accounting_lock(
                db,
                execution_id=execution_id,
                outcome=outcome,
                persist_provider_truth=None,
            )
        else:
            settled = await settle_risex_execution_under_accounting_lock(
                db,
                execution_id=execution_id,
                outcome=outcome,
                persist_provider_truth=persist_snapshot,
            )

        return {
            "provider_truth_persisted": True,
            "provider_truth_settled": settled.state in {
                ExecutionState.FILLED,
                ExecutionState.REJECTED,
                ExecutionState.CANCELED,
            },
            "execution_state": settled.state.value,
        }
