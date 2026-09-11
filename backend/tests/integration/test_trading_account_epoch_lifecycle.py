from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from starlette.requests import Request

from app.api import user as user_api
from app.db.session import SessionLocal
from app.models.entities import CopyJob, CopyState, ExecutionEpoch, JobState, User
from app.schemas.user import TradingAccountIn
from app.services.execution_destination import job_matches_active_destination, set_user_destination

pytestmark = pytest.mark.skipif(
    os.getenv('RUN_INTEGRATION') != '1',
    reason='requires CI PostgreSQL',
)


async def _insert_user(db, user_id: uuid.UUID, wallet: str) -> User:
    await db.execute(
        text(
            """
            INSERT INTO users (
                id, auth_wallet, role, state, copy_state, manual_trade_policy,
                execution_network, network_started_at, execution_provider,
                created_at, updated_at
            ) VALUES (
                :user_id, :wallet, 'USER', 'ACTIVE', 'PAUSED', 'COEXIST',
                'testnet', now(), 'hyperliquid', now(), now()
            )
            """
        ),
        {'user_id': user_id, 'wallet': wallet},
    )
    await db.flush()
    return (await db.execute(select(User).where(User.id == user_id))).scalar_one()


async def _cleanup_user(db, user_id: uuid.UUID) -> None:
    await db.rollback()
    await db.execute(
        text('UPDATE users SET active_execution_epoch_id = NULL WHERE id = :user_id'),
        {'user_id': user_id},
    )
    await db.execute(text('DELETE FROM users WHERE id = :user_id'), {'user_id': user_id})
    await db.commit()


def _request() -> Request:
    return Request({'type': 'http', 'client': ('127.0.0.1', 12345), 'headers': []})


def _stub_trading_account_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = '0x' + ('ab' * 20)

    class FakeFollower:
        async def verify_agent(self, account_address, private_key, *, expected_agent_address=None):
            del account_address, private_key, expected_agent_address
            return SimpleNamespace(agent_address=agent, name='test-agent', valid_until=None)

    async def fake_audit(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(user_api, '_follower_hl', lambda _network: FakeFollower())
    monkeypatch.setattr(
        user_api,
        'crypto',
        SimpleNamespace(
            encrypt=lambda *_args, **_kwargs: SimpleNamespace(
                ciphertext_b64='ciphertext',
                nonce_b64='nonce',
                wrapped_dek_b64='wrapped',
                wrap_nonce_b64='wrapnonce',
                key_provider='test',
                key_reference='test-key',
                key_version=1,
            )
        ),
    )
    monkeypatch.setattr(user_api, 'audit', fake_audit)


@pytest.mark.asyncio
async def test_missing_credential_version_rotates_open_epoch() -> None:
    user_id = uuid.uuid4()
    wallet = '0x' + uuid.uuid4().hex + uuid.uuid4().hex[:8]
    async with SessionLocal() as db:
        try:
            await _insert_user(db, user_id, wallet)
            first = await set_user_destination(
                db,
                user_id,
                provider='hyperliquid',
                network='testnet',
                account_address=wallet,
                credential_version=1,
            )
            second = await set_user_destination(
                db,
                user_id,
                provider='hyperliquid',
                network='testnet',
                account_address=wallet,
                credential_version=None,
            )
            assert second.epoch_id != first.epoch_id
        finally:
            await _cleanup_user(db, user_id)


@pytest.mark.asyncio
async def test_missing_account_address_rotates_open_epoch() -> None:
    user_id = uuid.uuid4()
    wallet = '0x' + uuid.uuid4().hex + uuid.uuid4().hex[:8]
    async with SessionLocal() as db:
        try:
            await _insert_user(db, user_id, wallet)
            first = await set_user_destination(
                db,
                user_id,
                provider='hyperliquid',
                network='testnet',
                account_address=wallet,
                credential_version=1,
            )
            second = await set_user_destination(
                db,
                user_id,
                provider='hyperliquid',
                network='testnet',
                account_address=None,
                credential_version=1,
            )
            assert second.epoch_id != first.epoch_id
        finally:
            await _cleanup_user(db, user_id)


@pytest.mark.asyncio
async def test_exact_identity_reuses_open_epoch() -> None:
    user_id = uuid.uuid4()
    wallet = '0x' + uuid.uuid4().hex + uuid.uuid4().hex[:8]
    async with SessionLocal() as db:
        try:
            await _insert_user(db, user_id, wallet)
            first = await set_user_destination(
                db,
                user_id,
                provider='hyperliquid',
                network='testnet',
                account_address=wallet,
                credential_version=1,
            )
            second = await set_user_destination(
                db,
                user_id,
                provider='hyperliquid',
                network='testnet',
                account_address=wallet,
                credential_version=1,
            )
            assert second.epoch_id == first.epoch_id
        finally:
            await _cleanup_user(db, user_id)


@pytest.mark.asyncio
async def test_trading_account_replacement_rotates_epoch_and_rejects_queued_job(monkeypatch) -> None:
    _stub_trading_account_dependencies(monkeypatch)
    user_id = uuid.uuid4()
    wallet = '0x' + uuid.uuid4().hex + uuid.uuid4().hex[:8]
    body = TradingAccountIn(agent_private_key='1' * 64)

    async with SessionLocal() as db:
        try:
            user = await _insert_user(db, user_id, wallet)
            await user_api.link_trading_account(body, _request(), user, db)
            first_epoch = (
                await db.execute(text('SELECT active_execution_epoch_id FROM users WHERE id = :user_id'), {'user_id': user_id})
            ).scalar_one()
            job = CopyJob(
                user_id=user_id,
                asset='BTC',
                origin='RECONCILE',
                state=JobState.QUEUED,
                correlation_id=uuid.uuid4().hex,
                execution_epoch_id=first_epoch,
                execution_provider='hyperliquid',
                execution_network='testnet',
                context={'follower_network': 'testnet'},
            )
            db.add(job)
            await db.commit()

            await user_api.link_trading_account(body, _request(), user, db)
            second_epoch = (
                await db.execute(text('SELECT active_execution_epoch_id FROM users WHERE id = :user_id'), {'user_id': user_id})
            ).scalar_one()

            assert second_epoch != first_epoch
            assert await job_matches_active_destination(db, job) is False
        finally:
            await _cleanup_user(db, user_id)


@pytest.mark.asyncio
async def test_trading_account_unlink_closes_epoch_and_rejects_queued_job(monkeypatch) -> None:
    _stub_trading_account_dependencies(monkeypatch)
    user_id = uuid.uuid4()
    wallet = '0x' + uuid.uuid4().hex + uuid.uuid4().hex[:8]
    body = TradingAccountIn(agent_private_key='2' * 64)

    async with SessionLocal() as db:
        try:
            user = await _insert_user(db, user_id, wallet)
            await user_api.link_trading_account(body, _request(), user, db)
            epoch_id = (
                await db.execute(text('SELECT active_execution_epoch_id FROM users WHERE id = :user_id'), {'user_id': user_id})
            ).scalar_one()
            job = CopyJob(
                user_id=user_id,
                asset='ETH',
                origin='RECONCILE',
                state=JobState.QUEUED,
                correlation_id=uuid.uuid4().hex,
                execution_epoch_id=epoch_id,
                execution_provider='hyperliquid',
                execution_network='testnet',
                context={'follower_network': 'testnet'},
            )
            db.add(job)
            await db.commit()

            await user_api.unlink_trading_account(user, db)
            active_epoch = (
                await db.execute(text('SELECT active_execution_epoch_id FROM users WHERE id = :user_id'), {'user_id': user_id})
            ).scalar_one()
            ended_at = (
                await db.execute(text('SELECT ended_at FROM execution_epochs WHERE id = :epoch_id'), {'epoch_id': epoch_id})
            ).scalar_one()

            assert active_epoch is None
            assert ended_at is not None
            assert user.copy_state == CopyState.PAUSED
            assert await job_matches_active_destination(db, job) is False
        finally:
            await _cleanup_user(db, user_id)


@pytest.mark.asyncio
async def test_initial_trading_account_link_materializes_epoch_with_credential_identity(monkeypatch) -> None:
    _stub_trading_account_dependencies(monkeypatch)
    user_id = uuid.uuid4()
    wallet = '0x' + uuid.uuid4().hex + uuid.uuid4().hex[:8]
    body = TradingAccountIn(agent_private_key='3' * 64)

    async with SessionLocal() as db:
        try:
            user = await _insert_user(db, user_id, wallet)
            await user_api.link_trading_account(body, _request(), user, db)

            epoch = (
                await db.execute(
                    select(ExecutionEpoch).join(User, User.active_execution_epoch_id == ExecutionEpoch.id).where(User.id == user_id)
                )
            ).scalar_one()
            assert epoch.account_address == wallet.lower()
            assert epoch.credential_version == 1
        finally:
            await _cleanup_user(db, user_id)
