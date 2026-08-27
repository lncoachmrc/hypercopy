from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.entities import CopyJob, JobState
from app.services.master_leverage_cache import (
    publish_master_leverage_repair,
    record_master_leverage_repaired,
)


STRATEGY_ORIGINS = {'EVENT', 'RECONCILE'}
_HF006_REPAIR_PENDING = 'hf006_repair_pending'
_HF006_REPAIR_ACCOUNTED_ORDER = 'hf006_repair_accounted_order'


async def ensure_group(redis: Redis) -> None:
    try:
        await redis.xgroup_create(settings.STREAM_NAME, settings.STREAM_GROUP, id='0', mkstream=True)
    except Exception as exc:
        if 'BUSYGROUP' not in str(exc):
            raise


async def publish_job(redis: Redis, db: AsyncSession, job: CopyJob) -> None:
    await redis.xadd(settings.STREAM_NAME, {'job_id': str(job.id)}, maxlen=100_000, approximate=True)
    job.enqueued_at = datetime.now(UTC)
    await db.flush()


def _parsed_master_leverage(ctx: dict) -> int | None:
    raw = ctx.get('master_leverage')
    if raw in (None, ''):
        return None
    try:
        return max(1, int(Decimal(str(raw))))
    except Exception:
        return None


def reconcile_job_repairs_missing_leverage(job: CopyJob) -> bool:
    if job.origin != 'RECONCILE':
        return False
    ctx = job.context or {}
    try:
        master_position = Decimal(str(ctx.get('master_position', '0') or '0'))
    except Exception:
        return False
    if master_position == 0:
        return True
    return _parsed_master_leverage(ctx) is not None


def reconcile_job_repair_evidence(job: CopyJob) -> int | None:
    """Return only conservative shared-order evidence for a repair job."""

    if not reconcile_job_repairs_missing_leverage(job):
        return None
    raw = (job.context or {}).get('master_snapshot_started_order')
    try:
        value = Decimal(str(raw))
    except Exception:
        return None
    if not value.is_finite() or value <= 0 or value != value.to_integral_value():
        return None
    return int(value)


def mark_hf006_repair_pending(job: CopyJob) -> bool:
    """Persist a repair-accounting obligation before PostgreSQL fallback execution.

    The flag lives inside the durable CopyJob JSON context, so no schema change is
    required. Assigning a fresh dict is intentional: SQLAlchemy then persists the
    JSON mutation without relying on mutable-extension instrumentation.
    """

    evidence_order = reconcile_job_repair_evidence(job)
    if evidence_order is None:
        return False
    ctx = dict(job.context or {})
    if ctx.get(_HF006_REPAIR_PENDING) is True:
        return False
    ctx[_HF006_REPAIR_PENDING] = True
    ctx.pop(_HF006_REPAIR_ACCOUNTED_ORDER, None)
    job.context = ctx
    return True


def hf006_repair_pending(job: CopyJob) -> bool:
    return bool((job.context or {}).get(_HF006_REPAIR_PENDING) is True)


def mark_hf006_repair_accounted(job: CopyJob, evidence_order: int) -> None:
    ctx = dict(job.context or {})
    ctx[_HF006_REPAIR_PENDING] = False
    ctx[_HF006_REPAIR_ACCOUNTED_ORDER] = int(evidence_order)
    job.context = ctx


async def replay_completed_hf006_repairs(
    redis: Redis,
    db: AsyncSession,
    *,
    limit: int = 500,
) -> int:
    """Replay only HF-006 bookkeeping for DONE jobs completed via DB fallback.

    A PostgreSQL-fallback worker may complete a corrective RECONCILE while Redis
    is unavailable. Those jobs must never be republished after completion; only
    their durable repair obligation is replayed once Redis is available again.
    Successful Redis execution (including the no-active-marker case) is enough
    to clear the obligation because the repair watermark has then been recorded.
    """

    rows = (
        await db.execute(
            select(CopyJob)
            .where(
                CopyJob.state == JobState.DONE,
                CopyJob.origin == 'RECONCILE',
                CopyJob.context[_HF006_REPAIR_PENDING].as_boolean().is_(True),
            )
            .order_by(CopyJob.updated_at, CopyJob.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()

    accounted = 0
    for job in rows:
        evidence_order = reconcile_job_repair_evidence(job)
        if evidence_order is None:
            # Corrupt/malformed evidence remains visibly pending instead of
            # being silently acknowledged without authoritative proof.
            continue
        await record_master_leverage_repaired(
            redis,
            job.user_id,
            job.asset,
            evidence_order=evidence_order,
        )
        mark_hf006_repair_accounted(job, evidence_order)
        accounted += 1

    if accounted:
        await db.flush()
    return accounted


def stale_enqueue_cutoff(now: datetime | None = None) -> datetime:
    current = now or datetime.now(UTC)
    return current - timedelta(seconds=max(settings.JOB_LEASE_SECONDS, 30))


def strategy_job_expiry_cutoff(now: datetime | None = None) -> datetime:
    current = now or datetime.now(UTC)
    return current - timedelta(seconds=settings.STRATEGY_JOB_MAX_AGE_SECONDS)


def strategy_job_expired(job: CopyJob, now: datetime | None = None) -> bool:
    if job.origin not in STRATEGY_ORIGINS:
        return False
    created_at = job.created_at
    if created_at is None:
        return False
    return created_at <= strategy_job_expiry_cutoff(now)


def expire_strategy_job(job: CopyJob) -> None:
    job.state = JobState.SKIPPED
    job.last_error = f'Stale strategy job expired after {settings.STRATEGY_JOB_MAX_AGE_SECONDS}s; current reconciliation supersedes it'
    job.owner = None
    job.locked_until = None
    job.next_attempt_at = None
    job.enqueued_at = None


async def expire_stale_strategy_jobs(db: AsyncSession, now: datetime | None = None, limit: int = 500) -> int:
    current = now or datetime.now(UTC)
    cutoff = strategy_job_expiry_cutoff(current)
    rows = (await db.execute(
        select(CopyJob).where(
            CopyJob.state.in_([JobState.QUEUED, JobState.RETRYING]),
            CopyJob.origin.in_(STRATEGY_ORIGINS),
            CopyJob.created_at <= cutoff,
        ).order_by(CopyJob.created_at).limit(limit).with_for_update(skip_locked=True)
    )).scalars().all()
    for job in rows:
        expire_strategy_job(job)
    if rows:
        await db.flush()
    return len(rows)


async def repair_stream(redis: Redis, db: AsyncSession, limit: int = 500) -> int:
    now = datetime.now(UTC)
    await expire_stale_strategy_jobs(db, now=now, limit=limit)

    # First discharge durable bookkeeping obligations left by PostgreSQL
    # fallback execution. This path never republishes a DONE order.
    await replay_completed_hf006_repairs(redis, db, limit=limit)

    stale_before = stale_enqueue_cutoff(now)
    rows = (await db.execute(
        select(CopyJob).where(
            CopyJob.state.in_([JobState.QUEUED, JobState.RETRYING]),
            or_(CopyJob.enqueued_at.is_(None), CopyJob.enqueued_at <= stale_before),
            (CopyJob.next_attempt_at.is_(None) | (CopyJob.next_attempt_at <= now)),
        ).order_by(CopyJob.created_at).limit(limit).with_for_update(skip_locked=True)
    )).scalars().all()
    count = 0
    for job in rows:
        evidence_order = reconcile_job_repair_evidence(job)
        if evidence_order is None:
            await publish_job(redis, db, job)
        else:
            # For an authoritative RECONCILE, publication and HF-006 recovery
            # bookkeeping are one Redis transaction. If Redis fails before the
            # script executes, neither happens. If PostgreSQL fallback wins the
            # race, its durable context flag is replayed above after completion.
            await publish_master_leverage_repair(
                redis,
                stream_name=settings.STREAM_NAME,
                job_id=job.id,
                user_id=job.user_id,
                asset=job.asset,
                evidence_order=evidence_order,
                maxlen=100_000,
            )
            if hf006_repair_pending(job):
                mark_hf006_repair_accounted(job, evidence_order)
            job.enqueued_at = datetime.now(UTC)
            await db.flush()
        count += 1
    await db.commit()
    return count
