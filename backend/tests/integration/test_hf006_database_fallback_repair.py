"""PostgreSQL regressions for HF-006 repair accounting through DB fallback."""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.core.config import settings
from app.db.session import SessionLocal, engine
from app.models.entities import CopyJob, CopyState, JobState, SystemFlag, User, UserState
from app.services.master_leverage_cache import next_master_leverage_causal_order
from app.services.queue import replay_completed_hf006_repairs
from app.workers.resilient_execution_worker import ResilientExecutionWorker

pytestmark = pytest.mark.skipif(
    os.getenv('RUN_INTEGRATION') != '1',
    reason='requires CI PostgreSQL',
)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_pool_after_test():
    yield
    await engine.dispose()


class BrokenMirrorRedis:
    async def set(self, *_args, **_kwargs):
        raise RuntimeError('redis unavailable')


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
        raise AssertionError('terminal fallback repair must never republish an order')


@pytest.mark.asyncio
async def test_causal_order_remains_durable_when_redis_is_unavailable(monkeypatch):
    master_address = '0x' + uuid.uuid4().hex + '00000000'
    monkeypatch.setattr(settings, 'HYPERLIQUID_MASTER_ADDRESS', master_address)

    first = await next_master_leverage_causal_order(BrokenMirrorRedis())
    second = await next_master_leverage_causal_order(BrokenMirrorRedis())

    assert first > 0
    assert second == first + 1

    slug = f'hf006_causal_order:{settings.master_network}:{master_address.lower()}'
    async with SessionLocal() as db:
        row = await db.get(SystemFlag, slug)
        assert row is not None
        assert row.enabled is True
        assert row.value['order'] == second


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal_state', [JobState.DONE, JobState.SKIPPED])
async def test_terminal_database_fallback_replays_repair_accounting_without_republishing(terminal_state):
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
        # Latest-intent fallback validation rejects strategy jobs from a stale
        # follower-network epoch. Keep this HF-006 fixture inside the active
        # epoch while still placing the job ahead of other queued test work.
        await db.execute(
            text(
                "UPDATE users SET execution_network = :network, "
                "network_started_at = now() - interval '10 minutes' "
                "WHERE id = :user_id"
            ),
            {'network': settings.follower_network, 'user_id': user_id},
        )
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
                    'master_intent_order': 20,
                    'master_network': settings.master_network,
                    'follower_network': settings.follower_network,
                },
            )
        )
        await db.commit()

    # Redis is unavailable, so the production worker selects the durable DB
    # fallback. The HF-006 accounting obligation must be committed BEFORE the
    # job can execute and transition to a terminal success/no-op state.
    worker = object.__new__(ResilientExecutionWorker)
    selected = await worker._next_database_job_id()
    assert selected == str(job_id)

    async with SessionLocal() as db:
        job = await db.get(CopyJob, job_id)
        assert job is not None
        assert job.context['hf006_repair_pending'] is True
        job.state = JobState.PROCESSING
        await db.flush()
        job.state = terminal_state
        await db.commit()

    # If Redis is still unavailable, the accounting obligation is not cleared.
    failing_redis = RepairReplayRedis(fail=True)
    async with SessionLocal() as db:
        with pytest.raises(RuntimeError, match='redis unavailable'):
            await replay_completed_hf006_repairs(failing_redis, db)
        await db.rollback()

    async with SessionLocal() as db:
        job = await db.get(CopyJob, job_id)
        assert job is not None
        assert job.state is terminal_state
        assert job.context['hf006_repair_pending'] is True

    # Once Redis returns, replay only the durable HF-006 bookkeeping obligation.
    # This helper never republishes the terminal corrective order to the stream.
    recovered_redis = RepairReplayRedis()
    async with SessionLocal() as db:
        accounted = await replay_completed_hf006_repairs(recovered_redis, db)
        assert accounted == 1
        await db.commit()

    assert recovered_redis.eval_calls == 1
    assert recovered_redis.xadd_calls == 0

    async with SessionLocal() as db:
        job = (
            await db.execute(select(CopyJob).where(CopyJob.id == job_id))
        ).scalar_one()
        assert job.state is terminal_state
        assert job.context['hf006_repair_pending'] is False
        assert job.context['hf006_repair_accounted_order'] == 20
