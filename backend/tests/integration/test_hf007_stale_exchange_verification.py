"""PostgreSQL regression for stale HF-007 exchange-verification attribution."""

import os
import uuid
from datetime import UTC, datetime, timedelta
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
from app.services.execution import LedgerApplicationDeferred, _apply_fill_to_ledger

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="requires CI PostgreSQL",
)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_pools_after_test():
    yield
    await engine.dispose()
    await position_ledger_lock_engine.dispose()


def _leg() -> SizingResult:
    return SizingResult(
        asset="BTC",
        intent=OrderIntent.OPEN,
        target_size=Decimal("1"),
        current_size=Decimal("0"),
        delta=Decimal("1"),
        order_size=Decimal("1"),
        is_buy=True,
        reduce_only=False,
        notional=Decimal("100"),
    )


@pytest.mark.asyncio
async def test_stale_exchange_verification_cannot_override_new_execution_attribution():
    user_id = uuid.uuid4()
    job_a_id = uuid.uuid4()
    job_b_id = uuid.uuid4()
    execution_a_id = uuid.uuid4()
    execution_b_id = uuid.uuid4()
    wallet = "0x" + uuid.uuid4().hex + "00000000"
    resolved_a = datetime.now(UTC)
    verified_after_a = resolved_a + timedelta(seconds=1)
    resolved_b = verified_after_a + timedelta(seconds=1)

    async with SessionLocal() as db:
        db.add(User(id=user_id, auth_wallet=wallet, state=UserState.SUSPENDED))
        await db.flush()
        db.add_all(
            [
                CopyJob(
                    id=job_a_id,
                    user_id=user_id,
                    asset="BTC",
                    origin="EVENT",
                    state=JobState.PROCESSING,
                    owner="hf007-a",
                    attempt_count=1,
                    correlation_id=uuid.uuid4().hex,
                    context={"follower_network": "testnet"},
                ),
                CopyJob(
                    id=job_b_id,
                    user_id=user_id,
                    asset="BTC",
                    origin="EVENT",
                    state=JobState.DONE,
                    attempt_count=1,
                    correlation_id=uuid.uuid4().hex,
                    context={"follower_network": "testnet"},
                ),
            ]
        )
        await db.flush()
        db.add_all(
            [
                Execution(
                    id=execution_a_id,
                    copy_job_id=job_a_id,
                    user_id=user_id,
                    attempt_kind="o",
                    cloid="0x" + uuid.uuid4().hex,
                    state=ExecutionState.FILLED,
                    asset="BTC",
                    is_buy=True,
                    requested_size=Decimal("1"),
                    reduce_only=False,
                    limit_px=Decimal("100"),
                    exchange_oid="hf007-a-oid",
                    filled_size=Decimal("0.4"),
                    avg_price=Decimal("100"),
                    resolved_at=resolved_a,
                    response={
                        "status": "ok",
                        "hf007": {
                            "ledger_size_before_submit": "0",
                            "ledger_last_execution_id_before_submit": None,
                        },
                    },
                ),
                Execution(
                    id=execution_b_id,
                    copy_job_id=job_b_id,
                    user_id=user_id,
                    attempt_kind="o",
                    cloid="0x" + uuid.uuid4().hex,
                    state=ExecutionState.FILLED,
                    asset="BTC",
                    is_buy=True,
                    requested_size=Decimal("0.4"),
                    reduce_only=False,
                    limit_px=Decimal("100"),
                    exchange_oid="hf007-b-oid",
                    filled_size=Decimal("0.4"),
                    avg_price=Decimal("100"),
                    resolved_at=resolved_b,
                    response={"status": "ok"},
                ),
            ]
        )
        await db.flush()
        db.add(
            PositionLedger(
                user_id=user_id,
                asset="BTC",
                size=Decimal("0.4"),
                target_size=Decimal("1"),
                mark_price=Decimal("100"),
                managed=True,
                last_execution_id=execution_b_id,
                exchange_verified_at=verified_after_a,
            )
        )
        await db.commit()

    async with SessionLocal() as db:
        job_a = await db.get(CopyJob, job_a_id)
        ledger = (
            await db.execute(
                select(PositionLedger).where(
                    PositionLedger.user_id == user_id,
                    PositionLedger.asset == "BTC",
                )
            )
        ).scalar_one()
        with pytest.raises(LedgerApplicationDeferred, match="attribution changed"):
            await _apply_fill_to_ledger(
                db,
                ledger,
                job_a,
                "o",
                _leg(),
                OrderOutcome("FILLED", "hf007-a-oid", Decimal("0.4"), Decimal("100")),
            )
        await db.refresh(ledger)
        execution_a = await db.get(Execution, execution_a_id)
        hf007 = (execution_a.response or {}).get("hf007", {})

        assert ledger.size == Decimal("0.4")
        assert ledger.last_execution_id == execution_b_id
        assert ledger.exchange_verified_at == verified_after_a
        assert not hf007.get("ledger_applied_at")
        assert hf007.get("ledger_apply_deferred_reason") == "ledger attribution changed since submit"
