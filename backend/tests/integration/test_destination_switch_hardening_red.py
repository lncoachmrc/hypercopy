from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.adapters.hyperliquid import HyperliquidAdapter
from app.db.session import SessionLocal, engine
from app.models.entities import (
    CopyJob,
    CopyState,
    Execution,
    ExecutionState,
    JobState,
    PositionLedger,
    User,
    UserState,
)
from app.services.execution_destination import close_user_destination_epoch, set_user_destination, user_destination_state


async def _insert_user(*, copy_state: CopyState) -> User:
    user_id = uuid.uuid4()
    wallet = '0x' + uuid.uuid4().hex + uuid.uuid4().hex[:8]
    async with SessionLocal() as db:
        user = User(
            id=user_id,
            auth_wallet=wallet,
            state=UserState.ACTIVE,
            copy_state=copy_state,
        )
        db.add(user)
        await db.commit()
    return user


async def _cleanup_user(user_id: uuid.UUID) -> None:
    async with SessionLocal() as db:
        await db.execute(
            text('UPDATE users SET active_execution_epoch_id = NULL WHERE id = :user_id'),
            {'user_id': user_id},
        )
        await db.execute(text('DELETE FROM users WHERE id = :user_id'), {'user_id': user_id})
        await db.commit()


async def _destination(user_id: uuid.UUID):
    async with SessionLocal() as db:
        return await user_destination_state(db, user_id)


async def _attempt_network_switch(user_id: uuid.UUID) -> None:
    async with SessionLocal() as db:
        try:
            await set_user_destination(
                db,
                user_id,
                provider='hyperliquid',
                network='mainnet',
            )
            await db.commit()
        except Exception:
            await db.rollback()


@pytest.mark.asyncio
async def test_direct_network_switch_is_blocked_when_user_is_not_paused() -> None:
    user = await _insert_user(copy_state=CopyState.ACTIVE)
    try:
        async with SessionLocal() as db:
            first = await set_user_destination(
                db,
                user.id,
                provider='hyperliquid',
                network='testnet',
            )
            await db.commit()

        await _attempt_network_switch(user.id)

        current = await _destination(user.id)
        assert current.epoch_id == first.epoch_id
        assert current.network == 'testnet'
    finally:
        await _cleanup_user(user.id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_never_activated_user_can_switch_without_provider_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden_provider_read(*_args, **_kwargs):
        raise AssertionError('NEVER_ACTIVATED must be proven from DB state before provider I/O')

    monkeypatch.setattr(HyperliquidAdapter, '_read', forbidden_provider_read)

    user = await _insert_user(copy_state=CopyState.PAUSED)
    try:
        async with SessionLocal() as db:
            first = await set_user_destination(
                db,
                user.id,
                provider='hyperliquid',
                network='testnet',
            )
            await db.commit()

        async with SessionLocal() as db:
            trading_account_count = (
                await db.execute(
                    text('SELECT count(*) FROM trading_accounts WHERE user_id = :user_id'),
                    {'user_id': user.id},
                )
            ).scalar_one()
            credential_count = (
                await db.execute(
                    text(
                        'SELECT count(*) FROM signing_credentials sc '
                        'JOIN trading_accounts ta ON ta.id = sc.trading_account_id '
                        'WHERE ta.user_id = :user_id'
                    ),
                    {'user_id': user.id},
                )
            ).scalar_one()
            epoch_account_count = (
                await db.execute(
                    text(
                        'SELECT count(*) FROM execution_epochs '
                        'WHERE user_id = :user_id AND account_address IS NOT NULL'
                    ),
                    {'user_id': user.id},
                )
            ).scalar_one()
            assert trading_account_count == 0
            assert credential_count == 0
            assert epoch_account_count == 0

            second = await set_user_destination(
                db,
                user.id,
                provider='hyperliquid',
                network='mainnet',
            )
            await db.commit()

        assert second.epoch_id != first.epoch_id
        assert second.network == 'mainnet'
    finally:
        await _cleanup_user(user.id)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('job_state', [JobState.QUEUED, JobState.PROCESSING, JobState.RETRYING])
async def test_pending_current_epoch_job_state_blocks_direct_network_switch(job_state: JobState) -> None:
    user = await _insert_user(copy_state=CopyState.PAUSED)
    try:
        async with SessionLocal() as db:
            first = await set_user_destination(
                db,
                user.id,
                provider='hyperliquid',
                network='testnet',
            )
            db.add(
                CopyJob(
                    user_id=user.id,
                    asset='BTC',
                    origin='RECONCILE',
                    state=job_state,
                    correlation_id=uuid.uuid4().hex,
                    execution_epoch_id=first.epoch_id,
                    execution_provider='hyperliquid',
                    execution_network='testnet',
                    context={'follower_network': 'testnet'},
                )
            )
            await db.commit()

        await _attempt_network_switch(user.id)

        current = await _destination(user.id)
        assert current.epoch_id == first.epoch_id
        assert current.network == 'testnet'
    finally:
        await _cleanup_user(user.id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_terminal_current_epoch_job_allows_network_switch() -> None:
    user = await _insert_user(copy_state=CopyState.PAUSED)
    try:
        async with SessionLocal() as db:
            first = await set_user_destination(
                db,
                user.id,
                provider='hyperliquid',
                network='testnet',
            )
            db.add(
                CopyJob(
                    user_id=user.id,
                    asset='BTC',
                    origin='RECONCILE',
                    state=JobState.DONE,
                    correlation_id=uuid.uuid4().hex,
                    execution_epoch_id=first.epoch_id,
                    execution_provider='hyperliquid',
                    execution_network='testnet',
                    context={'follower_network': 'testnet'},
                )
            )
            await db.commit()

        async with SessionLocal() as db:
            second = await set_user_destination(
                db,
                user.id,
                provider='hyperliquid',
                network='mainnet',
            )
            await db.commit()

        assert second.epoch_id != first.epoch_id
        assert second.network == 'mainnet'
    finally:
        await _cleanup_user(user.id)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('execution_state', [ExecutionState.SUBMITTING, ExecutionState.UNKNOWN])
async def test_unresolved_current_epoch_execution_blocks_network_switch(
    execution_state: ExecutionState,
) -> None:
    user = await _insert_user(copy_state=CopyState.PAUSED)
    try:
        async with SessionLocal() as db:
            first = await set_user_destination(
                db,
                user.id,
                provider='hyperliquid',
                network='testnet',
            )
            job = CopyJob(
                user_id=user.id,
                asset='BTC',
                origin='RECONCILE',
                state=JobState.DONE,
                correlation_id=uuid.uuid4().hex,
                execution_epoch_id=first.epoch_id,
                execution_provider='hyperliquid',
                execution_network='testnet',
                context={'follower_network': 'testnet'},
            )
            db.add(job)
            await db.flush()
            db.add(
                Execution(
                    copy_job_id=job.id,
                    user_id=user.id,
                    execution_epoch_id=first.epoch_id,
                    execution_provider='hyperliquid',
                    execution_network='testnet',
                    cloid='0x' + uuid.uuid4().hex,
                    state=execution_state,
                    asset='BTC',
                    is_buy=True,
                    requested_size=Decimal('0.001'),
                    reduce_only=False,
                    limit_px=Decimal('60000'),
                )
            )
            await db.commit()

        await _attempt_network_switch(user.id)

        current = await _destination(user.id)
        assert current.epoch_id == first.epoch_id
        assert current.network == 'testnet'
    finally:
        await _cleanup_user(user.id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_nonzero_managed_ledger_blocks_direct_network_switch() -> None:
    user = await _insert_user(copy_state=CopyState.PAUSED)
    try:
        async with SessionLocal() as db:
            first = await set_user_destination(
                db,
                user.id,
                provider='hyperliquid',
                network='testnet',
            )
            db.add(
                PositionLedger(
                    user_id=user.id,
                    asset='BTC',
                    size=Decimal('0.01'),
                    target_size=Decimal('0.01'),
                    managed=True,
                )
            )
            await db.commit()

        await _attempt_network_switch(user.id)

        current = await _destination(user.id)
        assert current.epoch_id == first.epoch_id
        assert current.network == 'testnet'
    finally:
        await _cleanup_user(user.id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_closed_activated_epoch_cannot_be_reinterpreted_as_first_bootstrap() -> None:
    user = await _insert_user(copy_state=CopyState.PAUSED)
    try:
        async with SessionLocal() as db:
            first = await set_user_destination(
                db,
                user.id,
                provider='hyperliquid',
                network='testnet',
                account_address=user.auth_wallet,
                credential_version=1,
            )
            await db.commit()

        async with SessionLocal() as db:
            await close_user_destination_epoch(db, user.id)
            await db.commit()

        await _attempt_network_switch(user.id)

        async with SessionLocal() as db:
            active_epoch_id = (
                await db.execute(
                    select(User.active_execution_epoch_id).where(User.id == user.id)
                )
            ).scalar_one()
            ended_at = (
                await db.execute(
                    text('SELECT ended_at FROM execution_epochs WHERE id = :epoch_id'),
                    {'epoch_id': first.epoch_id},
                )
            ).scalar_one()

        assert active_epoch_id is None
        assert ended_at is not None
    finally:
        await _cleanup_user(user.id)
        await engine.dispose()
