"""PostgreSQL regressions for P0 latest-intent-wins strategy execution."""

import os
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.db.position_ledger_lock import position_ledger_lock_engine
from app.db.session import SessionLocal, engine
from app.models.entities import (
    CopyJob,
    CopyState,
    Execution,
    ExecutionState,
    JobState,
    User,
    UserState,
)
from app.services.strategy_intents import (
    StrategyIntentAuthorizationError,
    StrategyIntentSuperseded,
    current_strategy_intent_for_cloid,
    prepare_strategy_job_for_publish,
)

pytestmark = pytest.mark.skipif(
    os.getenv('RUN_INTEGRATION') != '1',
    reason='requires CI PostgreSQL',
)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_pools_after_test():
    yield
    await engine.dispose()
    await position_ledger_lock_engine.dispose()


async def _user() -> uuid.UUID:
    user_id = uuid.uuid4()
    wallet = '0x' + uuid.uuid4().hex + '00000000'
    async with SessionLocal() as db:
        db.add(User(
            id=user_id,
            auth_wallet=wallet,
            state=UserState.ACTIVE,
            copy_state=CopyState.ACTIVE,
        ))
        await db.flush()
        await db.execute(
            text(
                "UPDATE users SET execution_network = 'testnet', "
                "network_started_at = now() - interval '1 minute' WHERE id = :user_id"
            ),
            {'user_id': user_id},
        )
        await db.commit()
    return user_id


def _context(order: int | None, position: str = '1') -> dict:
    out = {
        'follower_network': 'testnet',
        'master_network': 'mainnet',
        'master_position': position,
        'master_equity': '1000',
        'master_mark_price': '100',
    }
    if order is not None:
        out['master_intent_order'] = order
    return out


async def _add_job(
    user_id: uuid.UUID,
    *,
    order: int | None,
    origin: str,
    position: str = '1',
    with_execution: bool = False,
) -> tuple[uuid.UUID, str | None]:
    job_id = uuid.uuid4()
    cloid = '0x' + uuid.uuid4().hex if with_execution else None
    async with SessionLocal() as db:
        db.add(CopyJob(
            id=job_id,
            user_id=user_id,
            asset='BTC',
            origin=origin,
            state=JobState.QUEUED,
            correlation_id=uuid.uuid4().hex,
            context=_context(order, position),
        ))
        await db.flush()
        if cloid:
            db.add(Execution(
                copy_job_id=job_id,
                user_id=user_id,
                attempt_kind='o',
                cloid=cloid,
                state=ExecutionState.SUBMITTING,
                asset='BTC',
                is_buy=True,
                requested_size=Decimal('0.01'),
                reduce_only=False,
                limit_px=Decimal('100'),
            ))
        await db.commit()
    return job_id, cloid


@pytest.mark.asyncio
async def test_newer_reconcile_supersedes_older_event_at_action_boundary():
    user_id = await _user()
    _, old_cloid = await _add_job(
        user_id, order=10, origin='EVENT', position='1', with_execution=True,
    )
    await _add_job(user_id, order=11, origin='RECONCILE', position='0')

    assert old_cloid is not None
    with pytest.raises(StrategyIntentSuperseded, match='newer causal order 11'):
        await current_strategy_intent_for_cloid(
            cloid=old_cloid,
            follower_network='testnet',
            asset='BTC',
        )


@pytest.mark.asyncio
async def test_latest_strategy_intent_returns_durable_source_evidence():
    user_id = await _user()
    _, latest_cloid = await _add_job(
        user_id, order=21, origin='RECONCILE', position='0.125', with_execution=True,
    )

    assert latest_cloid is not None
    evidence = await current_strategy_intent_for_cloid(
        cloid=latest_cloid,
        follower_network='testnet',
        asset='BTC',
    )

    assert evidence is not None
    assert evidence.intent_order == 21
    assert evidence.source_master_position == Decimal('0.125')


@pytest.mark.asyncio
async def test_unversioned_strategy_job_is_fail_closed_before_submission():
    user_id = await _user()
    _, cloid = await _add_job(
        user_id, order=None, origin='EVENT', position='1', with_execution=True,
    )

    assert cloid is not None
    with pytest.raises(StrategyIntentAuthorizationError, match='unversioned'):
        await current_strategy_intent_for_cloid(
            cloid=cloid,
            follower_network='testnet',
            asset='BTC',
        )


@pytest.mark.asyncio
async def test_publish_coalescing_skips_older_event_and_reconcile_together():
    user_id = await _user()
    old_event_id, _ = await _add_job(user_id, order=30, origin='EVENT')
    old_reconcile_id, _ = await _add_job(user_id, order=31, origin='RECONCILE')
    latest_id, _ = await _add_job(user_id, order=32, origin='EVENT')

    async with SessionLocal() as db:
        latest = await db.get(CopyJob, latest_id)
        assert latest is not None
        assert await prepare_strategy_job_for_publish(db, latest) is True
        await db.commit()

    async with SessionLocal() as db:
        old_event = await db.get(CopyJob, old_event_id)
        old_reconcile = await db.get(CopyJob, old_reconcile_id)
        latest = await db.get(CopyJob, latest_id)
        assert old_event is not None and old_event.state == JobState.SKIPPED
        assert old_reconcile is not None and old_reconcile.state == JobState.SKIPPED
        assert latest is not None and latest.state == JobState.QUEUED
        assert 'Superseded by newer strategy intent 32' in (old_event.last_error or '')
        assert 'Superseded by newer strategy intent 32' in (old_reconcile.last_error or '')


@pytest.mark.asyncio
async def test_older_job_cannot_publish_when_newer_terminal_intent_exists():
    user_id = await _user()
    old_id, _ = await _add_job(user_id, order=40, origin='EVENT')
    newer_id, _ = await _add_job(user_id, order=41, origin='RECONCILE')

    async with SessionLocal() as db:
        newer = await db.get(CopyJob, newer_id)
        assert newer is not None
        newer.state = JobState.SKIPPED
        newer.last_error = 'Risk policy intentionally blocked latest intent'
        await db.commit()

    async with SessionLocal() as db:
        old = await db.get(CopyJob, old_id)
        assert old is not None
        assert await prepare_strategy_job_for_publish(db, old) is False
        await db.commit()

    async with SessionLocal() as db:
        old = await db.get(CopyJob, old_id)
        assert old is not None
        assert old.state == JobState.SKIPPED
        assert 'newer causal order 41' in (old.last_error or '')


@pytest.mark.asyncio
async def test_unversioned_job_is_quarantined_by_publish_coalescer():
    user_id = await _user()
    job_id, _ = await _add_job(user_id, order=None, origin='RECONCILE')

    async with SessionLocal() as db:
        job = await db.get(CopyJob, job_id)
        assert job is not None
        assert await prepare_strategy_job_for_publish(db, job) is False
        await db.commit()

    async with SessionLocal() as db:
        job = await db.get(CopyJob, job_id)
        assert job is not None
        assert job.state == JobState.SKIPPED
        assert 'unversioned' in (job.last_error or '')
