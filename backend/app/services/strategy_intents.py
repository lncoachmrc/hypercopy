from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Network
from app.db.session import SessionLocal
from app.models.entities import CopyJob, Execution, JobState
from app.services.networking import user_network_state


STRATEGY_ORIGINS = {'EVENT', 'RECONCILE'}
_ACTIVE_SUPERSEDE_STATES = {JobState.QUEUED, JobState.RETRYING}
_INTENT_SCAN_LIMIT = 512


class StrategyIntentAuthorizationError(RuntimeError):
    """A strategy order was definitively refused before exchange submission."""


class StrategyIntentSuperseded(StrategyIntentAuthorizationError):
    """A newer authoritative intent exists for the same follower market."""


@dataclass(frozen=True, slots=True)
class StrategyIntentEvidence:
    job_id: uuid.UUID
    user_id: uuid.UUID
    asset: str
    follower_network: Network
    intent_order: int
    source_master_position: Decimal


def _positive_intent_order(context: dict | None) -> int | None:
    raw = (context or {}).get('master_intent_order')
    if raw in (None, ''):
        return None
    try:
        parsed = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0 or parsed != parsed.to_integral_value():
        return None
    return int(parsed)


def _source_master_position(context: dict | None) -> Decimal | None:
    ctx = context or {}
    raw = ctx.get('source_master_position', ctx.get('master_position'))
    if raw in (None, ''):
        return None
    try:
        parsed = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def master_position_from_state(state: dict, asset: str) -> Decimal:
    """Extract the authoritative signed position for one perp market."""
    for row in state.get('assetPositions', []):
        position = row.get('position', row)
        if str(position.get('coin') or '') != asset:
            continue
        try:
            value = Decimal(str(position.get('szi', '0') or '0'))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise StrategyIntentAuthorizationError(
                f'Fresh master position for {asset} is malformed'
            ) from exc
        if not value.is_finite():
            raise StrategyIntentAuthorizationError(
                f'Fresh master position for {asset} is non-finite'
            )
        return value
    return Decimal(0)


def _job_evidence(job: CopyJob) -> StrategyIntentEvidence | None:
    if job.origin not in STRATEGY_ORIGINS:
        return None
    ctx = job.context or {}
    order = _positive_intent_order(ctx)
    source_position = _source_master_position(ctx)
    raw_network = str(ctx.get('follower_network') or '').lower()
    if raw_network not in {'testnet', 'mainnet'}:
        raise StrategyIntentAuthorizationError(
            'Strategy intent has no valid follower-network evidence'
        )
    if order is None:
        raise StrategyIntentAuthorizationError(
            'Strategy intent is unversioned; fresh reconciliation is required'
        )
    if source_position is None:
        raise StrategyIntentAuthorizationError(
            'Strategy intent has no valid source-position evidence'
        )
    return StrategyIntentEvidence(
        job_id=job.id,
        user_id=job.user_id,
        asset=job.asset,
        follower_network=raw_network,  # type: ignore[arg-type]
        intent_order=order,
        source_master_position=source_position,
    )


async def _intent_rows(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    asset: str,
    started_at,
) -> list[CopyJob]:
    return list((await db.execute(
        select(CopyJob).where(
            CopyJob.user_id == user_id,
            CopyJob.asset == asset,
            CopyJob.origin.in_(STRATEGY_ORIGINS),
            CopyJob.created_at >= started_at,
        ).order_by(CopyJob.created_at.desc()).limit(_INTENT_SCAN_LIMIT)
    )).scalars().all())


def _newer_intent_reason(
    current: CopyJob,
    current_order: int,
    rows: list[CopyJob],
) -> str | None:
    for candidate in rows:
        if candidate.id == current.id:
            continue
        candidate_order = _positive_intent_order(candidate.context)
        if candidate_order is not None and candidate_order > current_order:
            return (
                f'Strategy intent superseded by newer causal order '
                f'{candidate_order} (current {current_order})'
            )
        # An unversioned intent created after the current one destroys our ability
        # to prove that the current intent is still latest. Fail closed until a
        # later, versioned reconciliation restores an authoritative ordering.
        if (
            candidate_order is None
            and current.created_at is not None
            and candidate.created_at is not None
            and candidate.created_at > current.created_at
        ):
            return 'A newer unversioned strategy intent exists; fresh reconciliation is required'
    return None


def _mark_superseded(job: CopyJob, reason: str) -> None:
    job.state = JobState.SKIPPED
    job.last_error = reason
    job.owner = None
    job.locked_until = None
    job.next_attempt_at = None
    job.enqueued_at = None


async def prepare_strategy_job_for_publish(db: AsyncSession, job: CopyJob) -> bool:
    """Coalesce queued strategy work so only the newest user/asset intent publishes.

    PROCESSING jobs are deliberately not mutated here because they may already be
    near a signed action boundary. The independent pre-submit authorization fence
    rejects those jobs if this publication made them stale.
    """
    if job.origin not in STRATEGY_ORIGINS:
        return True

    await db.flush()
    try:
        evidence = _job_evidence(job)
    except StrategyIntentAuthorizationError as exc:
        _mark_superseded(job, str(exc))
        await db.flush()
        return False
    assert evidence is not None

    network_state = await user_network_state(db, job.user_id)
    if evidence.follower_network != network_state.network:
        _mark_superseded(job, 'Strategy intent belongs to a stale follower network')
        await db.flush()
        return False
    if job.created_at is not None and job.created_at < network_state.started_at:
        _mark_superseded(job, 'Strategy intent predates the current follower-network epoch')
        await db.flush()
        return False

    rows = await _intent_rows(
        db,
        user_id=job.user_id,
        asset=job.asset,
        started_at=network_state.started_at,
    )
    reason = _newer_intent_reason(job, evidence.intent_order, rows)
    if reason:
        _mark_superseded(job, reason)
        await db.flush()
        return False

    for candidate in rows:
        if candidate.id == job.id or candidate.state not in _ACTIVE_SUPERSEDE_STATES:
            continue
        candidate_order = _positive_intent_order(candidate.context)
        older = (
            candidate_order is not None and candidate_order < evidence.intent_order
        ) or (
            candidate_order is None
            and candidate.created_at is not None
            and job.created_at is not None
            and candidate.created_at <= job.created_at
        )
        if older:
            _mark_superseded(
                candidate,
                f'Superseded by newer strategy intent {evidence.intent_order}',
            )

    await db.flush()
    return True


async def current_strategy_intent_for_cloid(
    *,
    cloid: str,
    follower_network: Network,
    asset: str,
) -> StrategyIntentEvidence | None:
    """Authorize the durable strategy intent immediately before a signed order.

    The Execution row is committed before Hyperliquid is called, so its CLOID is
    a durable handle back to the CopyJob. Non-strategy actions such as CLOSE_ALL
    intentionally return ``None`` and retain their independent emergency path.
    """
    async with SessionLocal() as db:
        execution = (await db.execute(
            select(Execution).where(Execution.cloid == cloid)
        )).scalar_one_or_none()
        if execution is None:
            raise StrategyIntentAuthorizationError(
                'Durable execution evidence is missing before strategy submission'
            )
        job = await db.get(CopyJob, execution.copy_job_id)
        if job is None:
            raise StrategyIntentAuthorizationError(
                'Durable strategy job is missing before exchange submission'
            )
        if job.origin not in STRATEGY_ORIGINS:
            return None
        if job.asset != asset:
            raise StrategyIntentAuthorizationError(
                'Strategy intent asset does not match the signed order'
            )

        evidence = _job_evidence(job)
        assert evidence is not None
        network_state = await user_network_state(db, job.user_id)
        if evidence.follower_network != follower_network or network_state.network != follower_network:
            raise StrategyIntentSuperseded(
                'Strategy intent belongs to a stale follower network'
            )
        if job.created_at is not None and job.created_at < network_state.started_at:
            raise StrategyIntentSuperseded(
                'Strategy intent predates the current follower-network epoch'
            )

        rows = await _intent_rows(
            db,
            user_id=job.user_id,
            asset=job.asset,
            started_at=network_state.started_at,
        )
        reason = _newer_intent_reason(job, evidence.intent_order, rows)
        if reason:
            raise StrategyIntentSuperseded(reason)
        return evidence
