"""PostgreSQL regression for HF-006 repair accounting through DB fallback."""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.db.session import SessionLocal, engine
from app.models.entities import CopyJob, CopyState, JobState, User, UserState
from app.services.queue import repair_stream
from app.workers.resilient_execution_worker import ResilientExecutionWorker

pytestmark = pytest.mark.skipif(
    os.getenv('RUN_INTEGRATION') != '1',
    reason='requires CI PostgreSQL',
)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_pool_after_test():
    yield
    await engine.dispose()


class RepairReplayRedis:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.eval_calls = 0
        self.xadd_calls = 0

    async def eval(self, script, _numkeys, *_args):
        self.eval_calls += 1
        assert 'HF006_RECORD_REPAIR' in script
        if self.fail:
            raise RuntimeError('redis unavailable')
        # No active marker is also a successful bookkeeping replay: the Lua
        # records the durable repair watermark before returning false/nil.
        return None

    async def xadd(self, *_args, **_kwargs):
        self.xadd_calls += 1
        raise AssertionError('DONE fallback repair must never republish an order')


@pytest.mark.asyncio
async def test_done_database_fallback_replays_repair_accounting_without_republishing():
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()

    async with SessionLocal() as db:
        db.add(
            User(
                id=user_id,
                auth_wallet='0x' + uuid.uuid4().hex + '00000000',
                state=UserState.ACTIVE,
                copy_state=CopyState.ACTIVE,
            )
        )
        await db.flush()
        db.add(
            CopyJob(
                id=job_id,
                user_id=user_id,
                asset='BTC',
                origin='RECONCILE',
                state=JobState.QUEUED,
                correlation_id=uuid.uuid4().hex,
                # The integration database is intentionally shared across the
                # suite. Put this job clearly ahead of other live test jobs while
                # keeping it well inside the 600s strategy-expiry boundary.
                created_at=datetime.now(UTC) - timedelta(seconds=300),
                context={
                    'master_position': '1',
                    'master_leverage': 5,
                    'master_snapshot_started_order': 20,
                },
            )
        )
        await db.commit()

    # Redis is unavailable, so the production worker selects the durable DB
    # fallback. The HF-006 accounting obligation must be committed BEFORE the
    # job can be executed and transition to DONE.
    worker = object.__new__(ResilientExecutionWorker)
    selected = await worker._next_database_job_id()
    assert selected == str(job_id)

    async with SessionLocal() as db:
        job = await db.get(CopyJob, job_id)
        assert job is not None
        assert job.context['hf006_repair_pending'] is True
        job.state = JobState.PROCESSING
        await db.flush()
        job.state = JobState.DONE
        await db.commit()

    # If Redis is still unavailable, the accounting obligation is not cleared.
    failing_redis = RepairReplayRedis(fail=True)
    async with SessionLocal() as db:
        with pytest.raises(RuntimeError, match='redis unavailable'):
            await repair_stream(failing_redis, db)
        await db.rollback()

    async with SessionLocal() as db:
        job = await db.get(CopyJob, job_id)
        assert job is not None
        assert job.state is JobState.DONE
        assert job.context['hf006_repair_pending'] is True

    # Once Redis returns, repair_stream replays only the bookkeeping obligation.
    # The completed corrective order is never put back on the execution stream.
    recovered_redis = RepairReplayRedis()
    async with SessionLocal() as db:
        published = await repair_stream(recovered_redis, db)
        assert published == 0

    assert recovered_redis.eval_calls == 1
    assert recovered_redis.xadd_calls == 0

    async with SessionLocal() as db:
        job = (
            await db.execute(select(CopyJob).where(CopyJob.id == job_id))
        ).scalar_one()
        assert job.state is JobState.DONE
        assert job.context['hf006_repair_pending'] is False
        assert job.context['hf006_repair_accounted_order'] == 20
