"""PostgreSQL regression coverage for HF-007 crash/retry fill idempotency."""

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
from app.services.execution import (
    LedgerApplicationDeferred,
    _apply_fill_to_ledger,
    _execute_leg,
    claim_job,
)

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="requires CI PostgreSQL",
)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_pools_after_test():
    yield
    await engine.dispose()
    await position_ledger_lock_engine.dispose()


def _leg(*, current: str = "0", order_size: str = "1") -> SizingResult:
    current_size = Decimal(current)
    target = Decimal("1")
    size = Decimal(order_size)
    return SizingResult(
        asset="BTC",
        intent=OrderIntent.OPEN,
        target_size=target,
        current_size=current_size,
        delta=target - current_size,
        order_size=size,
        is_buy=True,
        reduce_only=False,
        notional=size * Decimal("100"),
    )


async def _seed_case(*, ledger_size: Decimal = Decimal("0")) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    wallet = "0x" + uuid.uuid4().hex + "00000000"

    async with SessionLocal() as db:
        db.add(
            User(
                id=user_id,
                auth_wallet=wallet,
                state=UserState.SUSPENDED,
            )
        )
        # These models do not have ORM relationships that let SQLAlchemy infer
        # the insert order from pending objects, so flush the FK parents exactly
        # as the production database requires.
        await db.flush()
        db.add(
            CopyJob(
                id=job_id,
                user_id=user_id,
                asset="BTC",
                origin="EVENT",
                state=JobState.PROCESSING,
                owner="hf007",
                attempt_count=1,
                correlation_id=uuid.uuid4().hex,
                context={"follower_network": "testnet"},
            )
        )
        await db.flush()
        db.add(
            Execution(
                id=execution_id,
                copy_job_id=job_id,
                user_id=user_id,
                attempt_kind="o",
                cloid="0x" + uuid.uuid4().hex,
                state=ExecutionState.FILLED,
                asset="BTC",
                is_buy=True,
                requested_size=Decimal("1"),
                reduce_only=False,
                limit_px=Decimal("100"),
                exchange_oid="hf007-oid",
                filled_size=Decimal("0.4"),
                avg_price=Decimal("100"),
                response={
                    "status": "ok",
                    "hf007": {"ledger_size_before_submit": "0"},
                },
            )
        )
        db.add(
            PositionLedger(
                user_id=user_id,
                asset="BTC",
                size=ledger_size,
                target_size=Decimal("1"),
                mark_price=Decimal("100"),
                managed=True,
            )
        )
        await db.commit()

    return user_id, job_id, execution_id


@pytest.mark.asyncio
async def test_crash_after_fill_application_does_not_apply_same_execution_twice():
    user_id, job_id, execution_id = await _seed_case()
    outcome = OrderOutcome(
        "FILLED",
        "hf007-oid",
        Decimal("0.4"),
        Decimal("100"),
        raw={"status": "ok"},
    )

    # First worker applies the confirmed exchange fill and then crashes before
    # it can finalize the CopyJob. Ledger update and the per-Execution marker
    # must be committed atomically.
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
        applied = await _apply_fill_to_ledger(db, ledger, job, "o", _leg(), outcome)
        assert applied is True
        await db.refresh(ledger)
        execution = await db.get(Execution, execution_id)
        assert ledger.size == Decimal("0.4")
        assert ledger.last_execution_id == execution_id
        assert (execution.response or {}).get("hf007", {}).get("ledger_applied_at")

        # Simulate lease recovery after the process dies before job finalization.
        job.state = JobState.RETRYING
        job.owner = None
        job.locked_until = None
        await db.commit()

    # The restarted worker claims the same durable job. `_execute_leg` must
    # rediscover the terminal Execution instead of submitting the CLOID again,
    # and fill application must be a no-op even though sizing would now see a
    # 0.600 residual.
    async with SessionLocal() as db:
        job = await claim_job(db, "hf007-restarted-worker", job_id)
        assert job is not None
        assert job.state == JobState.PROCESSING
        assert job.attempt_count == 2
        ledger = (
            await db.execute(
                select(PositionLedger).where(
                    PositionLedger.user_id == user_id,
                    PositionLedger.asset == "BTC",
                )
            )
        ).scalar_one()
        residual = _leg(current="0.4", order_size="0.6")
        rediscovered = await _execute_leg(
            db,
            object(),  # terminal Execution means no exchange adapter method is called
            job,
            user_id,
            "0x" + "1" * 40,
            "test-only-key-never-used",
            residual,
            Decimal("100"),
            50,
            "o",
        )
        assert rediscovered.state == "FILLED"
        assert rediscovered.filled_size == Decimal("0.4")
        applied = await _apply_fill_to_ledger(
            db,
            ledger,
            job,
            "o",
            residual,
            rediscovered,
        )
        assert applied is False
        await db.refresh(ledger)
        assert ledger.size == Decimal("0.4")


@pytest.mark.asyncio
async def test_exchange_snapshot_that_already_reflects_fill_is_not_incremented_again():
    user_id, job_id, execution_id = await _seed_case(ledger_size=Decimal("0.4"))
    outcome = OrderOutcome(
        "FILLED",
        "hf007-oid",
        Decimal("0.4"),
        Decimal("100"),
        raw={"status": "ok"},
    )

    # This models a crash after the exchange fill but before local delta
    # application, followed by reconciliation/observability refreshing the
    # ledger from authoritative exchange truth before the job is retried.
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
        applied = await _apply_fill_to_ledger(db, ledger, job, "o", _leg(), outcome)
        assert applied is False
        await db.refresh(ledger)
        execution = await db.get(Execution, execution_id)
        assert ledger.size == Decimal("0.4")
        assert (execution.response or {}).get("hf007", {}).get("ledger_applied_at")
        assert (execution.response or {}).get("hf007", {}).get("ledger_apply_mode") == "already_reflected"


@pytest.mark.asyncio
async def test_legacy_execution_without_pre_submit_evidence_fails_closed():
    user_id, job_id, execution_id = await _seed_case()
    async with SessionLocal() as db:
        execution = await db.get(Execution, execution_id)
        execution.response = {"status": "ok"}
        await db.commit()

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
        with pytest.raises(LedgerApplicationDeferred):
            await _apply_fill_to_ledger(
                db,
                ledger,
                job,
                "o",
                _leg(),
                OrderOutcome("FILLED", "hf007-oid", Decimal("0.4"), Decimal("100")),
            )
        await db.refresh(ledger)
        assert ledger.size == Decimal("0")


@pytest.mark.asyncio
async def test_unexpected_ledger_position_fails_closed_instead_of_adding_fill():
    user_id, job_id, _ = await _seed_case(ledger_size=Decimal("0.2"))
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
        with pytest.raises(LedgerApplicationDeferred):
            await _apply_fill_to_ledger(
                db,
                ledger,
                job,
                "o",
                _leg(),
                OrderOutcome("FILLED", "hf007-oid", Decimal("0.4"), Decimal("100")),
            )
        await db.refresh(ledger)
        assert ledger.size == Decimal("0.2")
