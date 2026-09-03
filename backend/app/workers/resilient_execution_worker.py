from __future__ import annotations

# Deployment trigger: reload shared P0 latest-intent safety modules in Railway staging.

import asyncio
import signal
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.models.entities import CopyJob, JobState
from app.services.queue import (
    ensure_group,
    expire_stale_strategy_jobs,
    mark_hf006_repair_pending,
)
from app.services.strategy_intents import STRATEGY_ORIGINS, prepare_strategy_job_for_publish
from app.workers.execution_worker import Worker, stop

settings.validate_for_service('execution-worker')
log = get_logger(__name__)


class ResilientExecutionWorker(Worker):
    """Execution worker with PostgreSQL as a durable queue fallback.

    Redis Streams remain the low-latency delivery path. CopyJob rows in
    PostgreSQL are the source of truth, so a transient Redis/group failure must
    never leave eligible jobs permanently QUEUED while the worker process still
    appears healthy.
    """

    async def _next_database_job_id(self) -> str | None:
        now = datetime.now(UTC)
        async with SessionLocal() as db:
            # Do not execute stale point-in-time strategy intents merely because
            # Redis delivery was delayed. Current reconciliation supersedes them.
            await expire_stale_strategy_jobs(db, now=now)
            await db.commit()

            while True:
                job = (
                    await db.execute(
                        select(CopyJob)
                        .where(
                            CopyJob.state.in_([JobState.QUEUED, JobState.RETRYING]),
                            (
                                CopyJob.next_attempt_at.is_(None)
                                | (CopyJob.next_attempt_at <= now)
                            ),
                        )
                        .order_by(CopyJob.created_at)
                        .limit(1)
                        .with_for_update(skip_locked=True)
                    )
                ).scalar_one_or_none()
                if job is None:
                    return None
                if job.origin not in STRATEGY_ORIGINS:
                    break
                if await prepare_strategy_job_for_publish(db, job):
                    break
                # The oldest intent was superseded or unversioned. Persist the
                # quarantine and keep scanning instead of executing stale work.
                await db.commit()

            # If this PostgreSQL fallback is about to execute an authoritative
            # HF-006 repair while Redis is unavailable, persist the accounting
            # obligation before execution can move the job to DONE. A later
            # repair_stream pass can then replay bookkeeping without republishing
            # the completed order.
            mark_hf006_repair_pending(job)
            await db.commit()
            return str(job.id)

    async def _drain_database_once(self) -> bool:
        job_id = await self._next_database_job_id()
        if not job_id:
            return False
        # Consuming from the durable queue is an expected degraded-mode action,
        # not itself an error. Redis unavailability is logged separately.
        log.info(
            'Consuming copy job through PostgreSQL fallback',
            extra={'job_id': job_id},
        )
        await self.handle_job_id(job_id)
        return True

    async def consume(self):
        group_ready = False
        while not stop.is_set():
            try:
                if not group_ready:
                    try:
                        await ensure_group(self.redis)
                        group_ready = True
                    except Exception:
                        # Previously this exception escaped before entering the
                        # consume loop, permanently killing the consumer task.
                        log.warning(
                            'Redis consumer group unavailable; using PostgreSQL fallback',
                            exc_info=True,
                        )
                        await self._drain_database_once()
                        await asyncio.sleep(2)
                        continue

                messages = await self.redis.xreadgroup(
                    settings.STREAM_GROUP,
                    self.id,
                    {settings.STREAM_NAME: '>'},
                    count=1,
                    block=2000,
                )
                if not messages:
                    if not await self._drain_database_once():
                        await self.heartbeat()
                    continue

                for _, entries in messages:
                    for message_id, data in entries:
                        ack = await self.handle_job_id(data.get('job_id', ''))
                        if ack:
                            await self.redis.xack(
                                settings.STREAM_NAME,
                                settings.STREAM_GROUP,
                                message_id,
                            )
            except Exception:
                # A Redis read/ack failure no longer stops execution progress.
                # Recreate/revalidate the group on the next pass and use the
                # durable DB queue in the meantime.
                group_ready = False
                log.exception('Worker consume loop failed; falling back to PostgreSQL')
                try:
                    await self._drain_database_once()
                except Exception:
                    log.exception('PostgreSQL fallback consumption failed')
                await asyncio.sleep(2)


async def main():
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await ResilientExecutionWorker().run()


if __name__ == '__main__':
    asyncio.run(main())
