from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.entities import CopyJob, JobState
from app.services.master_leverage_cache import record_master_leverage_repaired


STRATEGY_ORIGINS = {'EVENT', 'RECONCILE'}


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


def reconcile_job_repairs_missing_leverage(job: CopyJob) -> bool:
    if job.origin != 'RECONCILE':
        return False
    ctx = job.context or {}
    if ctx.get('master_leverage') is not None:
        return True
    try:
        return Decimal(str(ctx.get('master_position', '0') or '0')) == 0
    except Exception:
        return False


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
        await publish_job(redis, db, job)
        if reconcile_job_repairs_missing_leverage(job):
            try:
                evidence_created_at = job.created_at.timestamp() if job.created_at is not None else None
                await record_master_leverage_repaired(
                    redis,
                    job.user_id,
                    job.asset,
                    evidence_created_at=evidence_created_at,
                )
            except Exception:
                # Recovery telemetry must never prevent a durable correction
                # from reaching the execution stream.
                pass
        count += 1
    await db.commit()
    return count
