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
    CredentialStatus,
    Execution,
    ExecutionState,
    JobState,
    PositionLedger,
    SigningCredential,
    TradingAccount,
    User,
    UserState,
)
from app.services.execution_destination import close_user_destination_epoch, set_user_destination, user_destination_state

_SWITCH_TARGETS = (
    ('hyperliquid', 'mainnet'),
    ('risex', 'testnet'),
)


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


async def _attempt_destination_switch(
    user_id: uuid.UUID,
    *,
    provider: str,
    network: str,
) -> None:
    async with SessionLocal() as db:
        try:
            await set_user_destination(
                db,
                user_id,
                provider=provider,  # type: ignore[arg-type]
                network=network,  # type: ignore[arg-type]
            )
            await db.commit()
        except Exception:
            await db.rollback()


async def _activate_hyperliquid_user(user: User):
    async with SessionLocal() as db:
        account = TradingAccount(
            user_id=user.id,
            account_address=user.auth_wallet.lower(),
            agent_address='0x' + ('ab' * 20),
            agent_name='safe-switch-test',
        )
        db.add(account)
        await db.flush()
        db.add(
            SigningCredential(
                trading_account_id=account.id,
                ciphertext_b64='ciphertext',
                nonce_b64='nonce',
                wrapped_dek_b64='wrapped',
                wrap_nonce_b64='wrapnonce',
                key_provider='test',
                key_reference='test-key',
                key_version=1,
                agent_fingerprint='f' * 64,
                status=CredentialStatus.ACTIVE,
            )
        )
        first = await set_user_destination(
            db,
            user.id,
            provider='hyperliquid',
            network='testnet',
            account_address=user.auth_wallet.lower(),
            credential_version=1,
        )
        await db.commit()
        return first


@pytest.mark.asyncio
@pytest.mark.parametrize(('target_provider', 'target_network'), _SWITCH_TARGETS)
async def test_direct_destination_switch_is_blocked_when_user_is_not_paused(
    target_provider: str,
    target_network: str,
) -> None:
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

        await _attempt_destination_switch(
            user.id,
            provider=target_provider,
            network=target_network,
        )

        current = await _destination(user.id)
        assert current.epoch_id == first.epoch_id
        assert current.provider == 'hyperliquid'
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
@pytest.mark.parametrize(('target_provider', 'target_network'), _SWITCH_TARGETS)
async def test_pending_current_epoch_job_state_blocks_direct_destination_switch(
    job_state: JobState,
    target_provider: str,
    target_network: str,
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

        await _attempt_destination_switch(
            user.id,
            provider=target_provider,
            network=target_network,
        )

        current = await _destination(user.id)
        assert current.epoch_id == first.epoch_id
        assert current.provider == 'hyperliquid'
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
@pytest.mark.parametrize(('target_provider', 'target_network'), _SWITCH_TARGETS)
async def test_unresolved_current_epoch_execution_blocks_destination_switch(
    execution_state: ExecutionState,
    target_provider: str,
    target_network: str,
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

        await _attempt_destination_switch(
            user.id,
            provider=target_provider,
            network=target_network,
        )

        current = await _destination(user.id)
        assert current.epoch_id == first.epoch_id
        assert current.provider == 'hyperliquid'
        assert current.network == 'testnet'
    finally:
        await _cleanup_user(user.id)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(('target_provider', 'target_network'), _SWITCH_TARGETS)
async def test_nonzero_managed_ledger_blocks_direct_destination_switch(
    target_provider: str,
    target_network: str,
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

        await _attempt_destination_switch(
            user.id,
            provider=target_provider,
            network=target_network,
        )

        current = await _destination(user.id)
        assert current.epoch_id == first.epoch_id
        assert current.provider == 'hyperliquid'
        assert current.network == 'testnet'
    finally:
        await _cleanup_user(user.id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_provider_nonzero_position_blocks_activated_hyperliquid_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def provider_state(*_args, **_kwargs):
        return {'assetPositions': [{'position': {'coin': 'BTC', 'szi': '0.01'}}]}

    async def provider_orders(*_args, **_kwargs):
        return []

    monkeypatch.setattr(HyperliquidAdapter, 'user_state', provider_state)
    monkeypatch.setattr(HyperliquidAdapter, 'frontend_open_orders', provider_orders)

    user = await _insert_user(copy_state=CopyState.PAUSED)
    try:
        first = await _activate_hyperliquid_user(user)
        await _attempt_destination_switch(user.id, provider='hyperliquid', network='mainnet')
        current = await _destination(user.id)
        assert current.epoch_id == first.epoch_id
        assert current.network == 'testnet'
    finally:
        await _cleanup_user(user.id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_provider_trigger_order_blocks_activated_hyperliquid_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def provider_state(*_args, **_kwargs):
        return {'assetPositions': []}

    async def provider_orders(*_args, **_kwargs):
        return [
            {
                'coin': 'BTC',
                'oid': 99,
                'isTrigger': True,
                'triggerPx': '61000',
                'isPositionTpsl': True,
                'orderType': 'Take Profit Market',
            }
        ]

    monkeypatch.setattr(HyperliquidAdapter, 'user_state', provider_state)
    monkeypatch.setattr(HyperliquidAdapter, 'frontend_open_orders', provider_orders)

    user = await _insert_user(copy_state=CopyState.PAUSED)
    try:
        first = await _activate_hyperliquid_user(user)
        await _attempt_destination_switch(user.id, provider='hyperliquid', network='mainnet')
        current = await _destination(user.id)
        assert current.epoch_id == first.epoch_id
        assert current.network == 'testnet'
    finally:
        await _cleanup_user(user.id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_provider_read_failure_blocks_activated_hyperliquid_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def provider_state(*_args, **_kwargs):
        raise TimeoutError('provider timeout')

    async def provider_orders(*_args, **_kwargs):
        raise AssertionError('order read must not run after position read failure')

    monkeypatch.setattr(HyperliquidAdapter, 'user_state', provider_state)
    monkeypatch.setattr(HyperliquidAdapter, 'frontend_open_orders', provider_orders)

    user = await _insert_user(copy_state=CopyState.PAUSED)
    try:
        first = await _activate_hyperliquid_user(user)
        await _attempt_destination_switch(user.id, provider='hyperliquid', network='mainnet')
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

        await _attempt_destination_switch(user.id, provider='hyperliquid', network='mainnet')

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
