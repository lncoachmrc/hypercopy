"""Durable, idempotent SHADOW PnL telemetry; never places or queues orders."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import AIProfitExitObservation


def _number(value) -> Decimal | None:
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return d if d.is_finite() else None


def observation_key(
    *,
    user_id: uuid.UUID,
    execution_epoch_id: uuid.UUID,
    execution_provider: str,
    execution_network: str,
    asset: str,
    source_cycle_id: str,
    state_version: int,
    evaluation_slot: int,
    follower_position_size: Decimal | None,
) -> uuid.UUID:
    """One durable immutable snapshot per stable source state and clock slot."""
    position = _number(follower_position_size)
    signed_size = str(position.normalize()) if position is not None else "unavailable"
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        "|".join((
            "traxion:ai-profit-exit:shadow-observation:v1",
            str(user_id), str(execution_epoch_id),
            execution_provider, execution_network, asset,
            source_cycle_id, str(state_version), str(evaluation_slot), signed_size,
        )),
    )


def shadow_observation_values(
    *,
    user_id: uuid.UUID,
    execution_epoch_id: uuid.UUID,
    execution_provider: str,
    execution_network: str,
    asset: str,
    source_cycle_id: str,
    state_version: int,
    evaluation_slot: int,
    observation,
    position_matches: bool,
) -> dict:
    """Capture observed evidence even for negative, zero or missing PnL."""
    position = _number(observation.current_position)
    economics = observation.economics
    pnl = _number(economics.net_pnl) if economics is not None else None
    complete = bool(observation.complete)
    matches = bool(position_matches)
    eligible = bool(complete and matches and observation.eligible and pnl is not None and pnl > 0)
    return {
        "id": observation_key(
            user_id=user_id, execution_epoch_id=execution_epoch_id,
            execution_provider=execution_provider, execution_network=execution_network,
            asset=asset, source_cycle_id=source_cycle_id,
            state_version=state_version, evaluation_slot=evaluation_slot,
            follower_position_size=position,
        ),
        "user_id": user_id,
        "execution_epoch_id": execution_epoch_id,
        "execution_provider": execution_provider,
        "execution_network": execution_network,
        "asset": asset,
        "source_cycle_id": source_cycle_id,
        "state_version": state_version,
        "evaluation_slot": evaluation_slot,
        "follower_position_size": position,
        "evaluation_basis": str(observation.basis)[:32],
        "pnl_complete": bool(observation.pnl_complete),
        "funding_model": str(observation.funding_model)[:40],
        "mark_price": _number(observation.mark_price),
        "executable_exit_price": _number(observation.executable_exit_price),
        "entry_price": _number(observation.entry_price),
        "net_pnl": pnl,
        "complete": complete,
        "eligible": eligible,
        "position_matches": matches,
        "reason": str(observation.reason or "")[:240],
        "observed_at": datetime.now(UTC),
    }


async def record_shadow_observation(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    execution_epoch_id: uuid.UUID,
    execution_provider: str,
    execution_network: str,
    asset: str,
    source_cycle_id: str,
    state_version: int,
    evaluation_slot: int,
    observation,
    position_matches: bool,
) -> None:
    """Commit the snapshot before the positive-only decision gate.

    PostgreSQL UPSERT is append-only: repeated workers/evaluations cannot
    overwrite an earlier observation for the same slot. No network writes.
    """
    values = shadow_observation_values(
        user_id=user_id,
        execution_epoch_id=execution_epoch_id,
        execution_provider=execution_provider,
        execution_network=execution_network,
        asset=asset,
        source_cycle_id=source_cycle_id,
        state_version=state_version,
        evaluation_slot=evaluation_slot,
        observation=observation,
        position_matches=position_matches,
    )
    stmt = pg_insert(AIProfitExitObservation).values(**values).on_conflict_do_nothing(
        index_elements=[AIProfitExitObservation.id],
    )
    await db.execute(stmt)
    await db.commit()
