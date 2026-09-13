from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import text

import app.api.user as user_api
import app.schemas.user as user_schemas
from app.adapters.hyperliquid import HyperliquidAdapter
from app.core.config import settings
from app.db.session import SessionLocal, engine
from app.models.entities import (
    CopyJob,
    CopyState,
    CredentialStatus,
    JobState,
    PositionLedger,
    RiskState,
    SigningCredential,
    TradingAccount,
    User,
    UserState,
)
from app.services.execution_destination import (
    job_matches_active_destination,
    set_user_destination,
    user_destination_state,
)
from app.services.networking import set_user_network

pytestmark = pytest.mark.skipif(
    os.getenv('RUN_INTEGRATION') != '1',
    reason='requires CI PostgreSQL',
)


def _wallet() -> str:
    return '0x' + uuid.uuid4().hex + uuid.uuid4().hex[:8]


async def _insert_user(*, copy_state: CopyState = CopyState.PAUSED) -> uuid.UUID:
    async with SessionLocal() as db:
        user = User(auth_wallet=_wallet(), state=UserState.ACTIVE, copy_state=copy_state)
        db.add(user)
        await db.commit()
        return user.id


async def _bootstrap_hyperliquid(user_id: uuid.UUID):
    async with SessionLocal() as db:
        await set_user_network(db, user_id, 'testnet')
        await db.commit()
        return await user_destination_state(db, user_id)


async def _cleanup_user(user_id: uuid.UUID) -> None:
    """Delete disposable fixtures unless immutable audit rows reference them."""
    async with SessionLocal() as db:
        audit_ref = (
            await db.execute(
                text(
                    'SELECT 1 FROM audit_logs '
                    'WHERE actor_id = :user_id OR subject_id = :user_id LIMIT 1'
                ),
                {'user_id': user_id},
            )
        ).scalar_one_or_none()
        if audit_ref is None:
            await db.execute(
                text('UPDATE users SET active_execution_epoch_id = NULL WHERE id = :user_id'),
                {'user_id': user_id},
            )
            await db.execute(text('DELETE FROM users WHERE id = :user_id'), {'user_id': user_id})
            await db.commit()
        else:
            # Audit rows are append-only and intentionally retain their principal
            # references. The integration database is ephemeral, so preserving
            # this random UUID fixture is safer than mutating immutable evidence.
            await db.rollback()
    await engine.dispose()


async def _call_provider(db, user: User, provider: str):
    schema = getattr(user_schemas, 'TradingProviderIn', None)
    assert schema is not None, 'TradingProviderIn schema is missing'
    endpoint = getattr(user_api, 'trading_provider', None)
    assert endpoint is not None, 'trading_provider endpoint is missing'
    return await endpoint(schema(provider=provider), user=user, db=db)


async def _activate_hyperliquid_user(user_id: uuid.UUID):
    async with SessionLocal() as db:
        user = await db.get(User, user_id)
        assert user is not None
        account = TradingAccount(
            user_id=user.id,
            account_address=user.auth_wallet.lower(),
            agent_address='0x' + ('ab' * 20),
            agent_name='provider-selection-test',
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
        destination = await set_user_destination(
            db,
            user.id,
            provider='hyperliquid',
            network='testnet',
            account_address=user.auth_wallet.lower(),
            credential_version=1,
        )
        await db.commit()
        return destination


@pytest.mark.asyncio
async def test_first_login_destination_default_remains_hyperliquid() -> None:
    user_id = await _insert_user()
    try:
        async with SessionLocal() as db:
            await set_user_network(db, user_id, settings.follower_network)
            await db.commit()
            destination = await user_destination_state(db, user_id)
        assert destination.provider == 'hyperliquid'
        assert destination.network == settings.follower_network
    finally:
        await _cleanup_user(user_id)


@pytest.mark.asyncio
async def test_never_activated_hyperliquid_switches_to_risex_in_new_epoch() -> None:
    user_id = await _insert_user()
    try:
        first = await _bootstrap_hyperliquid(user_id)
        async with SessionLocal() as db:
            user = await db.get(User, user_id)
            assert user is not None
            db.add(RiskState(user_id=user_id))
            await db.commit()

            payload = await _call_provider(db, user, 'risex')
            current = await user_destination_state(db, user_id)
            old = (
                await db.execute(
                    text(
                        'SELECT provider, network, account_address, ended_at '
                        'FROM execution_epochs WHERE id = :epoch_id'
                    ),
                    {'epoch_id': first.epoch_id},
                )
            ).mappings().one()
            risk_count = (
                await db.execute(
                    text('SELECT count(*) FROM risk_state WHERE user_id = :user_id'),
                    {'user_id': user_id},
                )
            ).scalar_one()
            audit_row = (
                await db.execute(
                    text(
                        'SELECT action, before, after FROM audit_logs '
                        'WHERE actor_id = :user_id AND subject_id = :user_id '
                        'ORDER BY ts DESC LIMIT 1'
                    ),
                    {'user_id': user_id},
                )
            ).mappings().one()

        assert payload['execution_provider'] == 'risex'
        assert payload['execution_network'] == 'testnet'
        assert payload['copy_state'] == CopyState.PAUSED.value
        assert current.epoch_id != first.epoch_id
        assert current.provider == 'risex'
        assert current.network == 'testnet'
        assert old['provider'] == 'hyperliquid'
        assert old['network'] == 'testnet'
        assert old['account_address'] is None
        assert old['ended_at'] is not None
        assert risk_count == 0
        assert audit_row['action'] == 'TRADING_PROVIDER_CHANGED'
        assert audit_row['before']['provider'] == 'hyperliquid'
        assert audit_row['after']['provider'] == 'risex'
        assert audit_row['before']['network'] == audit_row['after']['network'] == 'testnet'
        assert audit_row['before']['epoch_id'] == str(first.epoch_id)
        assert audit_row['after']['epoch_id'] == str(current.epoch_id)
    finally:
        await _cleanup_user(user_id)


@pytest.mark.asyncio
async def test_provider_selection_is_owner_scoped() -> None:
    user_a = await _insert_user()
    user_b = await _insert_user()
    try:
        first_a = await _bootstrap_hyperliquid(user_a)
        first_b = await _bootstrap_hyperliquid(user_b)
        async with SessionLocal() as db:
            actor = await db.get(User, user_a)
            assert actor is not None
            payload = await _call_provider(db, actor, 'risex')
            state_a = await user_destination_state(db, user_a)
            state_b = await user_destination_state(db, user_b)

        assert payload['id'] == str(user_a)
        assert state_a.provider == 'risex'
        assert state_a.epoch_id != first_a.epoch_id
        assert state_b.provider == 'hyperliquid'
        assert state_b.network == 'testnet'
        assert state_b.epoch_id == first_b.epoch_id
    finally:
        await _cleanup_user(user_a)
        await _cleanup_user(user_b)


@pytest.mark.asyncio
async def test_same_provider_is_idempotent_and_does_not_rotate_epoch() -> None:
    user_id = await _insert_user()
    try:
        first = await _bootstrap_hyperliquid(user_id)
        async with SessionLocal() as db:
            before_count = (
                await db.execute(
                    text('SELECT count(*) FROM execution_epochs WHERE user_id = :user_id'),
                    {'user_id': user_id},
                )
            ).scalar_one()
            user = await db.get(User, user_id)
            assert user is not None
            payload = await _call_provider(db, user, 'hyperliquid')
            after = await user_destination_state(db, user_id)
            after_count = (
                await db.execute(
                    text('SELECT count(*) FROM execution_epochs WHERE user_id = :user_id'),
                    {'user_id': user_id},
                )
            ).scalar_one()

        assert payload['execution_provider'] == 'hyperliquid'
        assert after.epoch_id == first.epoch_id
        assert after_count == before_count
    finally:
        await _cleanup_user(user_id)


@pytest.mark.asyncio
async def test_central_switch_block_is_translated_to_structured_409_without_epoch_mutation() -> None:
    user_id = await _insert_user(copy_state=CopyState.ACTIVE)
    try:
        first = await _bootstrap_hyperliquid(user_id)
        async with SessionLocal() as db:
            user = await db.get(User, user_id)
            assert user is not None
            with pytest.raises(HTTPException) as exc_info:
                await _call_provider(db, user, 'risex')
            await db.rollback()
            current = await user_destination_state(db, user_id)

        assert exc_info.value.status_code == 409
        assert isinstance(exc_info.value.detail, dict)
        assert exc_info.value.detail['code'] == 'destination_switch_blocked'
        assert 'pause_required' in {item['code'] for item in exc_info.value.detail['blockers']}
        assert current.epoch_id == first.epoch_id
        assert current.provider == 'hyperliquid'
    finally:
        await _cleanup_user(user_id)


@pytest.mark.asyncio
async def test_serialization_reports_canonical_provider_and_local_readiness_without_provider_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden_provider_read(*_args, **_kwargs):
        raise AssertionError('/me-style serialization must not perform live provider reads')

    monkeypatch.setattr(HyperliquidAdapter, 'user_state', forbidden_provider_read)
    monkeypatch.setattr(HyperliquidAdapter, 'frontend_open_orders', forbidden_provider_read)

    user_id = await _insert_user()
    try:
        await _bootstrap_hyperliquid(user_id)
        async with SessionLocal() as db:
            user = await db.get(User, user_id)
            assert user is not None
            payload = await user_api._serialize_user(db, user)

        assert payload['execution_provider'] == 'hyperliquid'
        assert payload['execution_network'] == 'testnet'
        assert payload['destination_switch_ready'] is True
        assert payload['destination_switch_blockers'] == []
        assert 'VERIFIED_FLAT' not in str(payload)
    finally:
        await _cleanup_user(user_id)


@pytest.mark.asyncio
async def test_activated_flat_hyperliquid_switches_to_risex_then_cleans_provider_local_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def provider_state(*_args, **_kwargs):
        return {'assetPositions': []}

    async def provider_orders(*_args, **_kwargs):
        return []

    monkeypatch.setattr(HyperliquidAdapter, 'user_state', provider_state)
    monkeypatch.setattr(HyperliquidAdapter, 'frontend_open_orders', provider_orders)

    user_id = await _insert_user()
    try:
        await _bootstrap_hyperliquid(user_id)
        activated = await _activate_hyperliquid_user(user_id)
        async with SessionLocal() as db:
            db.add(
                PositionLedger(
                    user_id=user_id,
                    asset='BTC',
                    size=Decimal('0'),
                    target_size=Decimal('0'),
                    managed=True,
                )
            )
            db.add(RiskState(user_id=user_id))
            await db.commit()
            user = await db.get(User, user_id)
            assert user is not None

            payload = await _call_provider(db, user, 'risex')
            current = await user_destination_state(db, user_id)
            account_count = (
                await db.execute(
                    text('SELECT count(*) FROM trading_accounts WHERE user_id = :user_id'),
                    {'user_id': user_id},
                )
            ).scalar_one()
            ledger_count = (
                await db.execute(
                    text('SELECT count(*) FROM position_ledger WHERE user_id = :user_id'),
                    {'user_id': user_id},
                )
            ).scalar_one()
            risk_count = (
                await db.execute(
                    text('SELECT count(*) FROM risk_state WHERE user_id = :user_id'),
                    {'user_id': user_id},
                )
            ).scalar_one()

        assert payload['execution_provider'] == 'risex'
        assert current.provider == 'risex'
        assert current.network == 'testnet'
        assert current.epoch_id != activated.epoch_id
        assert account_count == 0
        assert ledger_count == 0
        assert risk_count == 0
    finally:
        await _cleanup_user(user_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('provider_evidence', ['position', 'trigger', 'timeout'])
async def test_activated_hyperliquid_source_failures_are_blocked_by_central_boundary(
    monkeypatch: pytest.MonkeyPatch,
    provider_evidence: str,
) -> None:
    if provider_evidence == 'position':
        async def provider_state(*_args, **_kwargs):
            return {'assetPositions': [{'position': {'coin': 'BTC', 'szi': '0.01'}}]}

        async def provider_orders(*_args, **_kwargs):
            return []
    elif provider_evidence == 'trigger':
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
    else:
        async def provider_state(*_args, **_kwargs):
            raise TimeoutError('provider timeout')

        async def provider_orders(*_args, **_kwargs):
            raise AssertionError('order read must not run after position read failure')

    monkeypatch.setattr(HyperliquidAdapter, 'user_state', provider_state)
    monkeypatch.setattr(HyperliquidAdapter, 'frontend_open_orders', provider_orders)

    user_id = await _insert_user()
    try:
        await _bootstrap_hyperliquid(user_id)
        activated = await _activate_hyperliquid_user(user_id)
        async with SessionLocal() as db:
            user = await db.get(User, user_id)
            assert user is not None
            with pytest.raises(HTTPException) as exc_info:
                await _call_provider(db, user, 'risex')
            await db.rollback()
            current = await user_destination_state(db, user_id)
            account_count = (
                await db.execute(
                    text('SELECT count(*) FROM trading_accounts WHERE user_id = :user_id'),
                    {'user_id': user_id},
                )
            ).scalar_one()

        assert exc_info.value.status_code == 409
        assert current.epoch_id == activated.epoch_id
        assert current.provider == 'hyperliquid'
        assert account_count == 1
    finally:
        await _cleanup_user(user_id)


@pytest.mark.asyncio
async def test_activated_risex_source_without_complete_reads_is_fail_closed() -> None:
    user_id = await _insert_user()
    try:
        await _bootstrap_hyperliquid(user_id)
        async with SessionLocal() as db:
            risex = await set_user_destination(
                db,
                user_id,
                provider='risex',
                network='testnet',
                account_address=_wallet(),
                credential_version=1,
            )
            await db.commit()
            user = await db.get(User, user_id)
            assert user is not None
            with pytest.raises(HTTPException) as exc_info:
                await _call_provider(db, user, 'hyperliquid')
            await db.rollback()
            current = await user_destination_state(db, user_id)

        assert exc_info.value.status_code == 409
        assert current.epoch_id == risex.epoch_id
        assert current.provider == 'risex'
    finally:
        await _cleanup_user(user_id)


@pytest.mark.asyncio
async def test_never_activated_risex_can_switch_back_to_hyperliquid() -> None:
    user_id = await _insert_user()
    try:
        await _bootstrap_hyperliquid(user_id)
        async with SessionLocal() as db:
            user = await db.get(User, user_id)
            assert user is not None
            first_payload = await _call_provider(db, user, 'risex')
            risex = await user_destination_state(db, user_id)
            second_payload = await _call_provider(db, user, 'hyperliquid')
            hyperliquid = await user_destination_state(db, user_id)

        assert first_payload['execution_provider'] == 'risex'
        assert second_payload['execution_provider'] == 'hyperliquid'
        assert risex.provider == 'risex'
        assert hyperliquid.provider == 'hyperliquid'
        assert hyperliquid.network == risex.network == 'testnet'
        assert hyperliquid.epoch_id != risex.epoch_id
    finally:
        await _cleanup_user(user_id)


@pytest.mark.asyncio
async def test_pre_switch_terminal_job_is_stale_after_provider_switch() -> None:
    user_id = await _insert_user()
    try:
        first = await _bootstrap_hyperliquid(user_id)
        async with SessionLocal() as db:
            job = CopyJob(
                user_id=user_id,
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
            await db.commit()
            assert await job_matches_active_destination(db, job) is True

            user = await db.get(User, user_id)
            assert user is not None
            await _call_provider(db, user, 'risex')
            assert await job_matches_active_destination(db, job) is False
            current = await user_destination_state(db, user_id)

        assert current.provider == 'risex'
    finally:
        await _cleanup_user(user_id)
