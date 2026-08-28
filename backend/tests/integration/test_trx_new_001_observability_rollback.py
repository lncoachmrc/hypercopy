"""PostgreSQL regression for TRX-NEW-001 follower observability rollback handling."""

import os
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.db.position_ledger_lock import position_ledger_lock_engine
from app.db.session import SessionLocal, engine
from app.models.entities import CopyJob, CopyState, EquitySnapshot, TradingAccount, User, UserState
from app.workers.execution_worker import Worker

pytestmark = pytest.mark.skipif(
    os.getenv('RUN_INTEGRATION') != '1',
    reason='requires CI PostgreSQL',
)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_pools_after_test():
    yield
    await engine.dispose()
    await position_ledger_lock_engine.dispose()


class ObservabilityHL:
    network = 'testnet'

    def __init__(self, *, successful_address: str):
        self.successful_address = successful_address
        self.snapshot_calls: list[str] = []
        self.place_ioc_calls = 0

    async def mids(self):
        return {'BTC': '100'}

    async def account_snapshot(self, address: str):
        self.snapshot_calls.append(address)
        if address != self.successful_address:
            raise RuntimeError('snapshot unavailable')
        return SimpleNamespace(
            perp_state={'assetPositions': []},
            account_value=Decimal('100'),
            free_margin=Decimal('90'),
            collateral_balance=Decimal('100'),
            unrealized_pnl=Decimal('0'),
            abstraction='default',
        )

    async def place_ioc(self, *_args, **_kwargs):
        self.place_ioc_calls += 1
        raise AssertionError('observability fallback must never submit an order')


async def _next_two_free_low_user_ids() -> tuple[uuid.UUID, uuid.UUID]:
    async with SessionLocal() as db:
        existing = set((await db.execute(select(User.id))).scalars().all())

    candidate = 1
    selected: list[uuid.UUID] = []
    while len(selected) < 2:
        value = uuid.UUID(int=candidate)
        if value not in existing:
            selected.append(value)
        candidate += 1
    return selected[0], selected[1]


@pytest.mark.asyncio
async def test_observability_rollback_keeps_later_users_and_next_cycle_alive(monkeypatch):
    first_user_id, second_user_id = await _next_two_free_low_user_ids()
    first_wallet = '0x' + uuid.uuid4().hex + '00000000'
    second_wallet = '0x' + uuid.uuid4().hex + '00000000'
    first_agent = '0x' + uuid.uuid4().hex + '00000000'
    second_agent = '0x' + uuid.uuid4().hex + '00000000'

    async with SessionLocal() as db:
        db.add_all([
            User(
                id=first_user_id,
                auth_wallet=first_wallet,
                state=UserState.ACTIVE,
                copy_state=CopyState.ACTIVE,
            ),
            User(
                id=second_user_id,
                auth_wallet=second_wallet,
                state=UserState.ACTIVE,
                copy_state=CopyState.ACTIVE,
            ),
        ])
        await db.flush()
        for user_id in (first_user_id, second_user_id):
            await db.execute(
                text(
                    "UPDATE users SET execution_network = 'testnet', "
                    "network_started_at = now() - interval '1 minute' WHERE id = :user_id"
                ),
                {'user_id': user_id},
            )
        db.add_all([
            TradingAccount(
                user_id=first_user_id,
                account_address=first_wallet,
                agent_address=first_agent,
                agent_name='trx-new-001-first',
            ),
            TradingAccount(
                user_id=second_user_id,
                account_address=second_wallet,
                agent_address=second_agent,
                agent_name='trx-new-001-second',
            ),
        ])
        await db.commit()

    hl = ObservabilityHL(successful_address=second_wallet)
    worker = object.__new__(Worker)
    worker.followers = {'testnet': hl}

    rollback_calls = 0
    original_rollback = None

    async with SessionLocal() as db:
        session_type = type(db)
        original_rollback = session_type.rollback

        async def tracked_rollback(session):
            nonlocal rollback_calls
            rollback_calls += 1
            await original_rollback(session)

        monkeypatch.setattr(session_type, 'rollback', tracked_rollback)

        first_refreshed = await worker._refresh_follower_observability(db, 'testnet')
        second_refreshed = await worker._refresh_follower_observability(db, 'testnet')

    assert first_refreshed == 1
    assert second_refreshed == 1
    assert rollback_calls >= 2
    assert hl.place_ioc_calls == 0

    first_positions = [i for i, address in enumerate(hl.snapshot_calls) if address == first_wallet]
    second_positions = [i for i, address in enumerate(hl.snapshot_calls) if address == second_wallet]
    assert len(first_positions) == 2
    assert len(second_positions) == 2
    assert first_positions[0] < second_positions[0]
    assert first_positions[1] < second_positions[1]

    async with SessionLocal() as db:
        jobs = (
            await db.execute(
                select(CopyJob).where(CopyJob.user_id.in_([first_user_id, second_user_id]))
            )
        ).scalars().all()
        first_snapshots = (
            await db.execute(
                select(EquitySnapshot).where(EquitySnapshot.user_id == first_user_id)
            )
        ).scalars().all()
        second_snapshots = (
            await db.execute(
                select(EquitySnapshot).where(EquitySnapshot.user_id == second_user_id)
            )
        ).scalars().all()

    assert jobs == []
    assert first_snapshots == []
    assert len(second_snapshots) == 2
