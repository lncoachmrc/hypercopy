from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.db.session import SessionLocal
from app.services.execution_destination import user_destination_state
from app.services.networking import user_network_state

pytestmark = pytest.mark.skipif(
    os.getenv('RUN_INTEGRATION') != '1',
    reason='requires CI PostgreSQL',
)


@pytest.mark.asyncio
async def test_active_epoch_is_single_source_for_provider_and_network():
    user_id = uuid.uuid4()
    epoch_id = uuid.uuid4()
    wallet = '0x' + uuid.uuid4().hex[:40]
    started_at = datetime.now(UTC)

    async with SessionLocal() as db:
        try:
            await db.execute(
                text(
                    """
                    INSERT INTO users (
                        id, auth_wallet, role, state, copy_state, manual_trade_policy,
                        execution_network, network_started_at, execution_provider,
                        created_at, updated_at
                    ) VALUES (
                        :user_id, :wallet, 'USER', 'ACTIVE', 'SHADOW', 'COEXIST',
                        'mainnet', :started_at, 'risex', :started_at, :started_at
                    )
                    """
                ),
                {'user_id': user_id, 'wallet': wallet, 'started_at': started_at},
            )
            await db.execute(
                text(
                    """
                    INSERT INTO execution_epochs (
                        id, user_id, provider, network, account_address,
                        credential_version, started_at, ended_at
                    ) VALUES (
                        :epoch_id, :user_id, 'risex', 'mainnet', '0xabc',
                        7, :started_at, NULL
                    )
                    """
                ),
                {'epoch_id': epoch_id, 'user_id': user_id, 'started_at': started_at},
            )
            await db.execute(
                text('UPDATE users SET active_execution_epoch_id = :epoch_id WHERE id = :user_id'),
                {'epoch_id': epoch_id, 'user_id': user_id},
            )
            await db.commit()

            destination = await user_destination_state(db, user_id)
            network = await user_network_state(db, user_id)

            assert destination.provider == 'risex'
            assert destination.network == 'mainnet'
            assert destination.epoch_id == epoch_id
            assert destination.started_at == started_at
            assert network.network == destination.network
            assert network.started_at == destination.started_at
        finally:
            await db.execute(
                text('UPDATE users SET active_execution_epoch_id = NULL WHERE id = :user_id'),
                {'user_id': user_id},
            )
            await db.execute(text('DELETE FROM users WHERE id = :user_id'), {'user_id': user_id})
            await db.commit()


@pytest.mark.asyncio
async def test_network_compatibility_bootstraps_missing_destination_epoch_once():
    user_id = uuid.uuid4()
    wallet = '0x' + uuid.uuid4().hex[:40]
    started_at = datetime.now(UTC)

    async with SessionLocal() as db:
        try:
            await db.execute(
                text(
                    """
                    INSERT INTO users (
                        id, auth_wallet, role, state, copy_state, manual_trade_policy,
                        execution_network, network_started_at, execution_provider,
                        created_at, updated_at
                    ) VALUES (
                        :user_id, :wallet, 'USER', 'ACTIVE', 'SHADOW', 'COEXIST',
                        'mainnet', :started_at, 'hyperliquid', :started_at, :started_at
                    )
                    """
                ),
                {'user_id': user_id, 'wallet': wallet, 'started_at': started_at},
            )
            await db.commit()

            network = await user_network_state(db, user_id)
            destination = await user_destination_state(db, user_id)
            second = await user_network_state(db, user_id)

            assert network.network == 'mainnet'
            assert destination.provider == 'hyperliquid'
            assert destination.network == 'mainnet'
            assert destination.epoch_id == second_epoch_id(second, destination)

            active = (
                await db.execute(
                    text(
                        """
                        SELECT count(*)
                        FROM execution_epochs
                        WHERE user_id = :user_id AND ended_at IS NULL
                        """
                    ),
                    {'user_id': user_id},
                )
            ).scalar_one()
            assert active == 1
        finally:
            await db.execute(
                text('UPDATE users SET active_execution_epoch_id = NULL WHERE id = :user_id'),
                {'user_id': user_id},
            )
            await db.execute(text('DELETE FROM users WHERE id = :user_id'), {'user_id': user_id})
            await db.commit()


def second_epoch_id(second, destination):
    assert second.network == destination.network
    return destination.epoch_id
