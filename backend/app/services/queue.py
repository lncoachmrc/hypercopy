from __future__ import annotations

from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.entities import CopyJob, JobState


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


def stale_enqueue_cutoff(now: datetime | None = None) -> datetime:
    """Jobs older than this are safe to republish to Redis.

    PostgreSQL owns job state and ``claim_job`` is idempotent under row locking,
    so republishing a durable QUEUED/RETRYING job cannot execute it twice. This
    recovers messages stranded in a Redis consumer pending list when a Railway
    worker is replaced between XREADGROUP delivery and PostgreSQL claim/ack.
    """
    current = now or datetime.now(UTC)
    return current - timedelta(seconds=max(settings.JOB_LEASE_SECONDS, 30))


async def repair_stream(redis: Redis, db: AsyncSession, limit: int = 500) -> int:
    now = datetime.now(UTC)
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
        count += 1
    await db.commit()
    return count
