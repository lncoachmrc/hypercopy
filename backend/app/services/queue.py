from __future__ import annotations

from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import select
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


async def repair_stream(redis: Redis, db: AsyncSession, limit: int = 500) -> int:
    rows = (await db.execute(
        select(CopyJob).where(
            CopyJob.state.in_([JobState.QUEUED, JobState.RETRYING]),
            CopyJob.enqueued_at.is_(None),
            (CopyJob.next_attempt_at.is_(None) | (CopyJob.next_attempt_at <= datetime.now(UTC))),
        ).order_by(CopyJob.created_at).limit(limit).with_for_update(skip_locked=True)
    )).scalars().all()
    count = 0
    for job in rows:
        await publish_job(redis, db, job)
        count += 1
    await db.commit()
    return count
