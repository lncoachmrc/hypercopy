"""PostgreSQL regression coverage for HF-002 ledger serialization."""

import asyncio
import os
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.db.position_ledger_lock import position_ledger_lock
from app.db.session import SessionLocal, engine
from app.engine.sizing import AssetSpec, FollowerState, MasterExposure, plan
from app.models.entities import CopyState, PositionLedger, User

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="requires CI PostgreSQL",
)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_pool_after_test():
    """Do not carry asyncpg connections across pytest's per-test event loops."""
    yield
    await engine.dispose()


@pytest.mark.asyncio
async def test_reconcile_and_fill_writes_serialize_without_lost_update():
    user_id = uuid.uuid4()
    wallet = "0x" + uuid.uuid4().hex + "00000000"

    async with SessionLocal() as db:
        db.add(User(id=user_id, auth_wallet=wallet, copy_state=CopyState.ACTIVE))
        db.add(
            PositionLedger(
                user_id=user_id,
                asset="BTC",
                size=Decimal(1),
                target_size=Decimal(2),
                mark_price=Decimal(100),
                managed=True,
            )
        )
        await db.commit()

    reconcile_read = asyncio.Event()
    allow_reconcile_commit = asyncio.Event()
    fill_attempting_lock = asyncio.Event()
    fill_entered = asyncio.Event()

    async def reconciliation_writer():
        async with position_ledger_lock(user_id), SessionLocal() as db:
            ledger = (
                await db.execute(
                    select(PositionLedger).where(
                        PositionLedger.user_id == user_id,
                        PositionLedger.asset == "BTC",
                    )
                )
            ).scalar_one()
            assert ledger.size == Decimal(1)
            reconcile_read.set()
            await allow_reconcile_commit.wait()
            ledger.size = Decimal(1)
            await db.commit()

    async def execution_fill_writer():
        await reconcile_read.wait()
        fill_attempting_lock.set()
        async with position_ledger_lock(user_id):
            fill_entered.set()
            async with SessionLocal() as db:
                ledger = (
                    await db.execute(
                        select(PositionLedger).where(
                            PositionLedger.user_id == user_id,
                            PositionLedger.asset == "BTC",
                        )
                    )
                ).scalar_one()
                ledger.size += Decimal("0.25")
                await db.commit()

    reconciliation_task = asyncio.create_task(reconciliation_writer())
    await reconcile_read.wait()
    execution_task = asyncio.create_task(execution_fill_writer())
    try:
        await asyncio.wait_for(fill_attempting_lock.wait(), timeout=2)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(fill_entered.wait(), timeout=0.1)
    finally:
        allow_reconcile_commit.set()
        await asyncio.gather(reconciliation_task, execution_task, return_exceptions=True)

    async with SessionLocal() as db:
        final_size = (
            await db.execute(
                select(PositionLedger.size).where(
                    PositionLedger.user_id == user_id,
                    PositionLedger.asset == "BTC",
                )
            )
        ).scalar_one()

        sizing = plan(
            MasterExposure("BTC", Decimal(2), Decimal(100), Decimal(100)),
            FollowerState(str(user_id), Decimal(100), current_size=final_size),
            AssetSpec("BTC", 2, 50),
        )

        try:
            assert final_size == Decimal("1.25")
            assert sizing.delta == Decimal("0.75")
        finally:
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()
