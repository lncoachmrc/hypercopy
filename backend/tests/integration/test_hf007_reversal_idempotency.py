"""PostgreSQL regression for independent HF-007 reversal-leg accounting."""

import os
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.adapters.hyperliquid import OrderOutcome
from app.db.position_ledger_lock import position_ledger_lock_engine
from app.db.session import SessionLocal, engine
from app.engine.sizing import OrderIntent, SizingResult
from app.models.entities import (
    CopyJob,
    Execution,
    ExecutionState,
    JobState,
    PositionLedger,
    User,
    UserState,
)
from app.services.execution import _apply_fill_to_ledger

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="requires CI PostgreSQL",
)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_pools_after_test():
    yield
    await engine.dispose()
    await position_ledger_lock_engine.dispose()


def _leg(*, current: str, target: str, order_size: str, intent: OrderIntent) -> SizingResult:
    current_size = Decimal(current)
    target_size = Decimal(target)
    size = Decimal(order_size)
    return SizingResult(
        asset="BTC",
        intent=intent,
        target_size=target_size,
        current_size=current_size,
        delta=target_size - current_size,
        order_size=size,
        is_buy=False,
        reduce_only=intent in {OrderIntent.REDUCE, OrderIntent.CLOSE},
        notional=size * Decimal("100"),
    )


@pytest.mark.asyncio
async def test_reversal_close_and_open_have_independent_idempotency_markers():
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    close_execution_id = uuid.uuid4()
    open_execution_id = uuid.uuid4()
    wallet = "0x" + uuid.uuid4().hex + "00000000"

    async with SessionLocal() as db:
        db.add(User(id=user_id, auth_wallet=wallet, state=UserState.SUSPENDED))
        await db.flush()
        db.add(
            CopyJob(
                id=job_id,
                user_id=user_id,
                asset="BTC",
                origin="EVENT",
                state=JobState.PROCESSING,
                owner="hf007-reversal",
                attempt_count=1,
                correlation_id=uuid.uuid4().hex,
                context={"follower_network": "testnet"},
            )
        )
        await db.flush()
        db.add_all(
            [
                Execution(
                    id=close_execution_id,
                    copy_job_id=job_id,
                    user_id=user_id,
                    attempt_kind="c",
                    cloid="0x" + uuid.uuid4().hex,
                    state=ExecutionState.FILLED,
                    asset="BTC",
                    is_buy=False,
                    requested_size=Decimal("1"),
                    reduce_only=True,
                    limit_px=Decimal("100"),
                    exchange_oid="hf007-close",
                    filled_size=Decimal("1"),
                    avg_price=Decimal("100"),
                    response={"hf007": {"ledger_size_before_submit": "1"}},
                ),
                Execution(
                    id=open_execution_id,
                    copy_job_id=job_id,
                    user_id=user_id,
                    attempt_kind="o",
                    cloid="0x" + uuid.uuid4().hex,
                    state=ExecutionState.FILLED,
                    asset="BTC",
                    is_buy=False,
                    requested_size=Decimal("0.5"),
                    reduce_only=False,
                    limit_px=Decimal("100"),
                    exchange_oid="hf007-open",
                    filled_size=Decimal("0.5"),
                    avg_price=Decimal("100"),
                    response={"hf007": {"ledger_size_before_submit": "0"}},
                ),
            ]
        )
        db.add(
            PositionLedger(
                user_id=user_id,
                asset="BTC",
                size=Decimal("1"),
                target_size=Decimal("-0.5"),
                mark_price=Decimal("100"),
                managed=True,
            )
        )
        await db.commit()

    close_leg = _leg(
        current="1",
        target="0",
        order_size="1",
        intent=OrderIntent.CLOSE,
    )
    open_leg = _leg(
        current="0",
        target="-0.5",
        order_size="0.5",
        intent=OrderIntent.OPEN,
    )

    async with SessionLocal() as db:
        job = await db.get(CopyJob, job_id)
        ledger = (
            await db.execute(
                select(PositionLedger).where(
                    PositionLedger.user_id == user_id,
                    PositionLedger.asset == "BTC",
                )
            )
        ).scalar_one()

        # First leg closes the long and commits its own marker. A crash here
        # must not make the close applicable again, while the second leg is
        # still independently eligible for application.
        assert await _apply_fill_to_ledger(
            db,
            ledger,
            job,
            "c",
            close_leg,
            OrderOutcome("FILLED", "hf007-close", Decimal("1"), Decimal("100")),
        ) is True
        await db.refresh(ledger)
        assert ledger.size == Decimal("0")

        close_execution = await db.get(Execution, close_execution_id)
        open_execution = await db.get(Execution, open_execution_id)
        assert (close_execution.response or {})["hf007"].get("ledger_applied_at")
        assert not (open_execution.response or {})["hf007"].get("ledger_applied_at")

    # Restart/replay: close is a no-op; open short is applied exactly once.
    async with SessionLocal() as db:
        job = await db.get(CopyJob, job_id)
        ledger = (
            await db.execute(
                select(PositionLedger).where(
                    PositionLedger.user_id == user_id,
                    PositionLedger.asset == "BTC",
                )
            )
        ).scalar_one()

        assert await _apply_fill_to_ledger(
            db,
            ledger,
            job,
            "c",
            close_leg,
            OrderOutcome("FILLED", "hf007-close", Decimal("1"), Decimal("100")),
        ) is False
        assert ledger.size == Decimal("0")

        assert await _apply_fill_to_ledger(
            db,
            ledger,
            job,
            "o",
            open_leg,
            OrderOutcome("FILLED", "hf007-open", Decimal("0.5"), Decimal("100")),
        ) is True
        await db.refresh(ledger)
        assert ledger.size == Decimal("-0.5")

        assert await _apply_fill_to_ledger(
            db,
            ledger,
            job,
            "o",
            open_leg,
            OrderOutcome("FILLED", "hf007-open", Decimal("0.5"), Decimal("100")),
        ) is False
        await db.refresh(ledger)
        assert ledger.size == Decimal("-0.5")

        close_execution = await db.get(Execution, close_execution_id)
        open_execution = await db.get(Execution, open_execution_id)
        assert (close_execution.response or {})["hf007"].get("ledger_applied_at")
        assert (open_execution.response or {})["hf007"].get("ledger_applied_at")
