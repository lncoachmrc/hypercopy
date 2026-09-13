from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text

import app.api.user as user_api
from app.db.session import SessionLocal, engine
from app.models.entities import CopyState, User, UserState
from app.schemas.user import TradingProviderIn
from app.services.execution_destination import user_destination_state

pytestmark = pytest.mark.skipif(
    os.getenv('RUN_INTEGRATION') != '1',
    reason='requires CI PostgreSQL',
)


def _wallet() -> str:
    return '0x' + uuid.uuid4().hex + uuid.uuid4().hex[:8]


@pytest.mark.asyncio
async def test_never_activated_provider_switch_audit_uses_null_for_missing_epoch() -> None:
    user_id: uuid.UUID | None = None
    try:
        async with SessionLocal() as db:
            user = User(
                auth_wallet=_wallet(),
                state=UserState.ACTIVE,
                copy_state=CopyState.PAUSED,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
            user_id = user.id

            before = (
                await db.execute(
                    text(
                        'SELECT execution_provider, execution_network, active_execution_epoch_id '
                        'FROM users WHERE id = :user_id'
                    ),
                    {'user_id': user.id},
                )
            ).mappings().one()
            assert before['execution_provider'] == 'hyperliquid'
            assert before['execution_network'] == 'testnet'
            assert before['active_execution_epoch_id'] is None

            payload = await user_api.trading_provider(
                TradingProviderIn(provider='risex'),
                user=user,
                db=db,
            )
            current = await user_destination_state(db, user.id)
            audit_row = (
                await db.execute(
                    text(
                        "SELECT before, after FROM audit_logs "
                        "WHERE actor_id = :user_id AND subject_id = :user_id "
                        "AND action = 'TRADING_PROVIDER_CHANGED' "
                        'ORDER BY ts DESC LIMIT 1'
                    ),
                    {'user_id': user.id},
                )
            ).mappings().one()

        assert payload['execution_provider'] == 'risex'
        assert current.provider == 'risex'
        assert audit_row['before']['epoch_id'] is None
        assert audit_row['after']['epoch_id'] == str(current.epoch_id)
        assert audit_row['before']['epoch_id'] != 'None'
        assert audit_row['after']['epoch_id'] != 'None'
    finally:
        # Successful provider changes create immutable audit evidence that keeps
        # principal references. The CI database is ephemeral, so retain this
        # random fixture rather than mutating the append-only audit trail.
        await engine.dispose()
