"""Regression coverage for the additive execution-provider/epoch migration."""

from __future__ import annotations

import asyncio
import os
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings

pytestmark = pytest.mark.skipif(
    os.getenv('RUN_INTEGRATION') != '1',
    reason='requires CI PostgreSQL',
)

MIGRATION = Path('alembic/versions/0011_execution_destination_epochs.py')
REVISION_0010 = '0010_user_plan_discounts'


def _alembic(*args: str) -> None:
    subprocess.run(['alembic', *args], check=True)


async def _insert_legacy_user(user_id: uuid.UUID, wallet: str) -> None:
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO users (
                        id, auth_wallet, role, state, copy_state,
                        manual_trade_policy, execution_network, network_started_at,
                        created_at, updated_at
                    ) VALUES (
                        :id, :wallet, 'USER', 'ACTIVE', 'PAUSED',
                        'COEXIST', 'mainnet', now(), now(), now()
                    )
                    """
                ),
                {'id': user_id, 'wallet': wallet},
            )
    finally:
        await engine.dispose()


async def _read_backfill(user_id: uuid.UUID) -> tuple:
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        SELECT
                            u.execution_provider,
                            u.execution_network,
                            u.copy_state,
                            u.active_execution_epoch_id,
                            e.provider AS epoch_provider,
                            e.network AS epoch_network,
                            e.ended_at,
                            (
                                SELECT count(*)
                                FROM execution_epochs active
                                WHERE active.user_id = u.id
                                  AND active.ended_at IS NULL
                            ) AS active_epoch_count
                        FROM users u
                        JOIN execution_epochs e
                          ON e.id = u.active_execution_epoch_id
                        WHERE u.id = :user_id
                        """
                    ),
                    {'user_id': user_id},
                )
            ).one()
            return tuple(row)
    finally:
        await engine.dispose()


async def _cleanup(user_id: uuid.UUID) -> None:
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text('UPDATE users SET active_execution_epoch_id = NULL WHERE id = :user_id'),
                {'user_id': user_id},
            )
            await conn.execute(text('DELETE FROM users WHERE id = :user_id'), {'user_id': user_id})
    finally:
        await engine.dispose()


def test_0011_backfills_hyperliquid_epoch_without_changing_network_or_copy_state():
    assert MIGRATION.exists(), '0011 execution destination migration is missing'

    user_id = uuid.uuid4()
    wallet = '0x' + uuid.uuid4().hex[:40]

    _alembic('downgrade', REVISION_0010)
    try:
        asyncio.run(_insert_legacy_user(user_id, wallet))
        _alembic('upgrade', 'head')

        (
            provider,
            network,
            copy_state,
            active_epoch_id,
            epoch_provider,
            epoch_network,
            ended_at,
            active_epoch_count,
        ) = asyncio.run(_read_backfill(user_id))

        assert provider == 'hyperliquid'
        assert network == 'mainnet'
        assert copy_state == 'PAUSED'
        assert active_epoch_id is not None
        assert epoch_provider == 'hyperliquid'
        assert epoch_network == 'mainnet'
        assert ended_at is None
        assert active_epoch_count == 1
    finally:
        # Always restore the CI database to head for the rest of the suite.
        _alembic('upgrade', 'head')
        if MIGRATION.exists():
            asyncio.run(_cleanup(user_id))
