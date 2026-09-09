from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text

from app.db.session import SessionLocal, engine
from app.models.entities import CopyJob, CopyState, JobState, User, UserState
from app.services.execution_destination import set_user_destination

pytestmark = pytest.mark.skipif(
    os.getenv('RUN_INTEGRATION') != '1',
    reason='requires CI PostgreSQL',
)


async def _cleanup_user(db, user_id: uuid.UUID) -> None:
    await db.rollback()
    await db.execute(
        text('UPDATE users SET active_execution_epoch_id = NULL WHERE id = :user_id'),
        {'user_id': user_id},
    )
    await db.execute(text('DELETE FROM users WHERE id = :user_id'), {'user_id': user_id})
    await db.commit()


@pytest.mark.asyncio
async def test_job_from_previous_epoch_is_stale_even_when_provider_and_network_match():
    assert hasattr(CopyJob, 'execution_epoch_id')
    assert hasattr(CopyJob, 'execution_provider')
    assert hasattr(CopyJob, 'execution_network')

    from app.services.execution_destination import job_matches_active_destination

    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    wallet = '0x' + uuid.uuid4().hex + '00000000'

    try:
        async with SessionLocal() as db:
            try:
                db.add(
                    User(
                        id=user_id,
                        auth_wallet=wallet,
                        state=UserState.ACTIVE,
                        copy_state=CopyState.PAUSED,
                    )
                )
                await db.flush()

                first = await set_user_destination(
                    db,
                    user_id,
                    provider='hyperliquid',
                    network='testnet',
                )
                job = CopyJob(
                    id=job_id,
                    user_id=user_id,
                    asset='BTC',
                    origin='RECONCILE',
                    state=JobState.QUEUED,
                    correlation_id=uuid.uuid4().hex,
                    execution_epoch_id=first.epoch_id,
                    execution_provider=first.provider,
                    execution_network=first.network,
                    context={'follower_network': first.network},
                )
                db.add(job)
                await db.commit()

                assert await job_matches_active_destination(db, job) is True

                second = await set_user_destination(
                    db,
                    user_id,
                    provider='hyperliquid',
                    network='mainnet',
                )
                third = await set_user_destination(
                    db,
                    user_id,
                    provider='hyperliquid',
                    network='testnet',
                )
                assert first.epoch_id != second.epoch_id
                assert first.epoch_id != third.epoch_id
                assert third.provider == first.provider
                assert third.network == first.network

                assert await job_matches_active_destination(db, job) is False
            finally:
                await _cleanup_user(db, user_id)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unbound_hyperliquid_job_binds_only_inside_current_epoch():
    from app.services.execution_destination import bind_job_to_active_destination

    user_id = uuid.uuid4()
    wallet = '0x' + uuid.uuid4().hex + '00000000'

    try:
        async with SessionLocal() as db:
            try:
                db.add(
                    User(
                        id=user_id,
                        auth_wallet=wallet,
                        state=UserState.ACTIVE,
                        copy_state=CopyState.PAUSED,
                    )
                )
                await db.flush()
                first = await set_user_destination(
                    db,
                    user_id,
                    provider='hyperliquid',
                    network='testnet',
                )
                # Persist the epoch before creating legacy-style unbound jobs.
                # PostgreSQL now() is transaction-scoped, so this separate
                # transaction is required for created_at to prove the job was
                # created inside the active epoch.
                await db.commit()

                current_job = CopyJob(
                    user_id=user_id,
                    asset='BTC',
                    origin='CLOSE_ALL',
                    state=JobState.QUEUED,
                    correlation_id=uuid.uuid4().hex,
                    context={'follower_network': 'testnet', 'explicit_close': True},
                )
                stale_job = CopyJob(
                    user_id=user_id,
                    asset='ETH',
                    origin='RECONCILE',
                    state=JobState.QUEUED,
                    correlation_id=uuid.uuid4().hex,
                    context={'follower_network': 'testnet'},
                )
                db.add_all([current_job, stale_job])
                await db.commit()

                assert await bind_job_to_active_destination(db, current_job) is True
                assert current_job.execution_epoch_id == first.epoch_id
                assert current_job.execution_provider == 'hyperliquid'
                assert current_job.execution_network == 'testnet'
                assert (current_job.context or {}).get('execution_epoch_id') == str(first.epoch_id)
                assert (current_job.context or {}).get('provider_market') == 'BTC'

                await set_user_destination(
                    db,
                    user_id,
                    provider='hyperliquid',
                    network='mainnet',
                )
                current = await set_user_destination(
                    db,
                    user_id,
                    provider='hyperliquid',
                    network='testnet',
                )
                assert current.epoch_id != first.epoch_id

                assert await bind_job_to_active_destination(db, stale_job) is False
                assert stale_job.execution_epoch_id is None
                assert stale_job.execution_provider is None
                assert stale_job.execution_network is None
            finally:
                await _cleanup_user(db, user_id)
    finally:
        await engine.dispose()
