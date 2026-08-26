"""PostgreSQL regression coverage for HF-001 ambiguous execution recovery."""

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete, text

from app.core.config import settings
from app.db.position_ledger_lock import position_ledger_lock_engine
from app.db.session import SessionLocal, engine
from app.models.entities import (
    CopyJob,
    CopyState,
    Execution,
    ExecutionState,
    JobState,
    SystemIncident,
    TradingAccount,
    User,
)
from app.services.execution import _retry_or_dead
from app.services.execution_resolution import (
    UNKNOWN_EXECUTION_SLA_SECONDS,
    resolve_ambiguous_executions,
)

pytestmark = pytest.mark.skipif(
    os.getenv('RUN_INTEGRATION') != '1',
    reason='requires CI PostgreSQL',
)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_pools_after_test():
    yield
    # pytest-asyncio may give each test its own event loop. Both asyncpg pools
    # used by this regression must be drained before that loop is closed;
    # otherwise the next test can inherit a pooled connection owned by the old
    # loop and stall while acquiring the advisory-lock connection.
    await engine.dispose()
    await position_ledger_lock_engine.dispose()


class AmbiguousHL:
    network = 'testnet'

    def __init__(self, *, position: str = '0.5'):
        self.position = position
        self.place_ioc_calls = 0
        self.snapshot_calls = 0
        self.fill_calls = 0

    async def query_order_by_cloid(self, account: str, cloid: str):
        return {'status': 'unknownOid'}

    async def user_fills_by_time(self, account: str, start_ms: int):
        self.fill_calls += 1
        return []

    async def account_snapshot(self, account: str):
        self.snapshot_calls += 1
        return SimpleNamespace(
            perp_state={
                'assetPositions': [
                    {'position': {'coin': 'BTC', 'szi': self.position}},
                ]
            }
        )

    async def place_ioc(self, **kwargs):
        self.place_ioc_calls += 1
        raise AssertionError('HF-001 resolver must never submit an order')


class FilledHL(AmbiguousHL):
    async def query_order_by_cloid(self, account: str, cloid: str):
        return {
            'status': 'order',
            'order': {
                'status': 'filled',
                'order': {'oid': 123, 'origSz': '0.4'},
            },
        }

    async def user_fills_by_time(self, account: str, start_ms: int):
        self.fill_calls += 1
        return [
            {'oid': 123, 'sz': '0.1', 'px': '100'},
            {'oid': 123, 'sz': '0.3', 'px': '110'},
        ]

    async def account_snapshot(self, account: str):
        raise AssertionError('Terminal CLOID resolution should not need position quarantine')


async def _seed_ambiguous_execution(*, job_state: JobState, age_seconds: int):
    now = datetime.now(UTC)
    created_at = now - timedelta(seconds=age_seconds)
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    wallet = '0x' + uuid.uuid4().hex + '00000000'
    agent = '0x' + uuid.uuid4().hex + '00000000'

    async with SessionLocal() as db:
        db.add(
            User(
                id=user_id,
                auth_wallet=wallet,
                copy_state=CopyState.ACTIVE,
                created_at=created_at - timedelta(hours=1),
            )
        )
        await db.flush()
        await db.execute(
            text('UPDATE users SET execution_network = :network, network_started_at = :started WHERE id = :user_id'),
            {
                'network': 'testnet',
                'started': created_at - timedelta(minutes=1),
                'user_id': user_id,
            },
        )
        db.add(
            TradingAccount(
                user_id=user_id,
                account_address=wallet,
                agent_address=agent,
                agent_name='hf001-test',
            )
        )
        db.add(
            CopyJob(
                id=job_id,
                user_id=user_id,
                asset='BTC',
                origin='EVENT',
                state=job_state,
                context={'master_network': 'mainnet', 'follower_network': 'testnet'},
                attempt_count=settings.MAX_JOB_RETRIES,
                correlation_id=uuid.uuid4().hex,
                created_at=created_at,
            )
        )
        db.add(
            Execution(
                id=execution_id,
                copy_job_id=job_id,
                user_id=user_id,
                attempt_kind='o',
                cloid='0x' + uuid.uuid4().hex,
                state=ExecutionState.UNKNOWN,
                asset='BTC',
                is_buy=True,
                requested_size=Decimal('0.4'),
                reduce_only=False,
                limit_px=Decimal('100'),
                created_at=created_at,
            )
        )
        await db.commit()

    return user_id, job_id, execution_id


async def _cleanup(user_id: uuid.UUID):
    async with SessionLocal() as db:
        await db.execute(
            delete(SystemIncident).where(
                SystemIncident.code.in_([
                    'EXECUTION_UNKNOWN_AGED',
                    'EXECUTION_UNKNOWN_QUARANTINED',
                ])
            )
        )
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


@pytest.mark.asyncio
async def test_dead_unknown_is_quarantined_after_sla_without_blind_resubmit():
    user_id, job_id, execution_id = await _seed_ambiguous_execution(
        job_state=JobState.PROCESSING,
        age_seconds=UNKNOWN_EXECUTION_SLA_SECONDS + 30,
    )
    hl = AmbiguousHL(position='0.7')

    try:
        # Exercise the real retry exhaustion transition before invoking the
        # job-independent resolver.
        async with SessionLocal() as db:
            job = await db.get(CopyJob, job_id)
            assert job is not None
            assert await _retry_or_dead(db, job, 'Ambiguous execution', ambiguous=True) == JobState.DEAD.value

        async with SessionLocal() as db:
            result = await resolve_ambiguous_executions(db, hl)

        assert result['quarantined'] == 1
        assert hl.place_ioc_calls == 0
        assert hl.snapshot_calls == 1
        assert hl.fill_calls == 1

        async with SessionLocal() as db:
            execution = await db.get(Execution, execution_id)
            job = await db.get(CopyJob, job_id)
            assert execution is not None and job is not None
            assert job.state == JobState.DEAD
            assert execution.state == ExecutionState.QUARANTINED
            assert execution.resolved_at is not None
            assert execution.response['hf001']['resolution'] == 'QUARANTINED'
            assert execution.response['hf001']['exchange_position'] == '0.7'
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_aged_unknown_with_live_job_alerts_but_keeps_fence():
    user_id, _, execution_id = await _seed_ambiguous_execution(
        job_state=JobState.RETRYING,
        age_seconds=UNKNOWN_EXECUTION_SLA_SECONDS + 30,
    )
    hl = AmbiguousHL()

    try:
        async with SessionLocal() as db:
            result = await resolve_ambiguous_executions(db, hl)

        assert result['aged'] == 1
        assert result['quarantined'] == 0
        assert hl.place_ioc_calls == 0
        assert hl.snapshot_calls == 0

        async with SessionLocal() as db:
            execution = await db.get(Execution, execution_id)
            assert execution is not None
            assert execution.state == ExecutionState.UNKNOWN
            assert execution.resolved_at is None
            assert execution.response['hf001']['incident_id']
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_resolver_recovers_actual_fill_from_cloid_and_fill_history():
    user_id, _, execution_id = await _seed_ambiguous_execution(
        job_state=JobState.DEAD,
        age_seconds=60,
    )
    hl = FilledHL()

    try:
        async with SessionLocal() as db:
            result = await resolve_ambiguous_executions(db, hl)

        assert result['resolved'] == 1
        assert result['quarantined'] == 0
        assert hl.place_ioc_calls == 0
        assert hl.fill_calls == 1

        async with SessionLocal() as db:
            execution = await db.get(Execution, execution_id)
            assert execution is not None
            assert execution.state == ExecutionState.FILLED
            assert execution.exchange_oid == '123'
            assert execution.filled_size == Decimal('0.4')
            assert execution.avg_price == Decimal('107.5')
            assert execution.resolved_at is not None
    finally:
        await _cleanup(user_id)
