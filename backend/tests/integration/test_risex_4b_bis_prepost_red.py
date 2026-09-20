"""RED contracts for durable RISEx 4B-bis pre-POST persistence."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select

from app.db.session import SessionLocal, engine
from app.models.entities import (
    CopyJob,
    Execution,
    ExecutionEpoch,
    ExecutionState,
    JobState,
    User,
    UserState,
)
from app.services import risex_copy_execution

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="requires CI PostgreSQL",
)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_pool_after_test():
    yield
    await engine.dispose()


def _require(name: str):
    value = getattr(risex_copy_execution, name, None)
    assert value is not None, f"RED: expected app.services.risex_copy_execution.{name}"
    return value


async def _seed_job(*, suffix: str) -> tuple[uuid.UUID, uuid.UUID]:
    user_id = uuid.uuid4()
    epoch_id = uuid.uuid4()
    job_id = uuid.uuid4()
    wallet = "0x" + uuid.uuid4().hex + "00000000"

    async with SessionLocal() as db:
        db.add(User(id=user_id, auth_wallet=wallet, state=UserState.SUSPENDED))
        await db.flush()
        db.add(
            ExecutionEpoch(
                id=epoch_id,
                user_id=user_id,
                provider="risex",
                network="mainnet",
                account_address=f"risex-{suffix}",
                credential_version=1,
                started_at=datetime.now(UTC),
            )
        )
        await db.flush()
        db.add(
            CopyJob(
                id=job_id,
                user_id=user_id,
                execution_epoch_id=epoch_id,
                execution_provider="risex",
                execution_network="mainnet",
                asset="BTC",
                origin="EVENT",
                state=JobState.PROCESSING,
                owner=f"worker-{suffix}",
                attempt_count=1,
                correlation_id=uuid.uuid4().hex,
                context={"follower_network": "mainnet", "execution_provider": "risex"},
            )
        )
        await db.commit()
    return user_id, job_id


async def _cleanup(user_ids: list[uuid.UUID]) -> None:
    async with SessionLocal() as db:
        await db.execute(delete(Execution).where(Execution.user_id.in_(user_ids)))
        await db.execute(delete(CopyJob).where(CopyJob.user_id.in_(user_ids)))
        await db.execute(delete(ExecutionEpoch).where(ExecutionEpoch.user_id.in_(user_ids)))
        await db.commit()


@pytest.mark.asyncio
async def test_aggregate_check_and_execution_reservation_are_serialized_across_sessions():
    persist = _require("persist_risex_pre_post_execution")
    user_a, job_a_id = await _seed_job(suffix="a")
    user_b, job_b_id = await _seed_job(suffix="b")
    user_ids = [user_a, user_b]

    async def exposure_reader():
        # Both callers deliberately see the same provider truth. Correctness must
        # come from the shared PostgreSQL lock + already committed Execution reservation.
        return Decimal("0"), Decimal("2400")

    async def submit(job_id: uuid.UUID, client_order_id: int):
        async with SessionLocal() as db:
            job = await db.get(CopyJob, job_id)
            return await persist(
                db,
                job=job,
                cloid="0x" + uuid.uuid4().hex,
                client_order_id=client_order_id,
                requested_size=Decimal("1"),
                limit_px=Decimal("100"),
                is_buy=True,
                reduce_only=False,
                exposure_reader=exposure_reader,
                user_exposure_ceiling=Decimal("1000"),
                total_exposure_ceiling=Decimal("2500"),
            )

    try:
        results = await asyncio.gather(
            submit(job_a_id, 1001),
            submit(job_b_id, 1002),
            return_exceptions=True,
        )

        executions = [item for item in results if isinstance(item, Execution)]
        blocked = [item for item in results if isinstance(item, Exception)]

        assert len(executions) == 1
        assert len(blocked) == 1

        async with SessionLocal() as db:
            count = (
                await db.execute(
                    select(func.count())
                    .select_from(Execution)
                    .where(
                        Execution.execution_provider == "risex",
                        Execution.execution_network == "mainnet",
                        Execution.state.in_(
                            [ExecutionState.SUBMITTING, ExecutionState.UNKNOWN]
                        ),
                        Execution.user_id.in_(user_ids),
                    )
                )
            ).scalar_one()
            assert count == 1
    finally:
        await _cleanup(user_ids)


@pytest.mark.asyncio
async def test_execution_is_committed_before_post_identity_can_be_used_and_ambiguity_keeps_reservation():
    persist = _require("persist_risex_pre_post_execution")
    user_id, job_id = await _seed_job(suffix="single")

    async def exposure_reader():
        return Decimal("0"), Decimal("0")

    try:
        async with SessionLocal() as db:
            job = await db.get(CopyJob, job_id)
            execution = await persist(
                db,
                job=job,
                cloid="0x" + uuid.uuid4().hex,
                client_order_id=424242,
                requested_size=Decimal("0.5"),
                limit_px=Decimal("200"),
                is_buy=True,
                reduce_only=False,
                exposure_reader=exposure_reader,
                user_exposure_ceiling=Decimal("1000"),
                total_exposure_ceiling=Decimal("2500"),
            )
            execution_id = execution.id

        # A fresh session must see the row before any provider POST is attempted.
        async with SessionLocal() as db:
            execution = await db.get(Execution, execution_id)
            assert execution is not None
            assert execution.state == ExecutionState.SUBMITTING
            assert execution.execution_provider == "risex"
            assert execution.execution_network == "mainnet"
            assert execution.execution_epoch_id is not None
            assert execution.client_order_id == Decimal("424242")
            assert execution.requested_size == Decimal("0.5")
            assert execution.limit_px == Decimal("200")

            # UNKNOWN remains a live reservation; it cannot be interpreted as released.
            execution.state = ExecutionState.UNKNOWN
            await db.commit()

        reserved = _require("reserved_exposure_usdc")
        async with SessionLocal() as db:
            execution = await db.get(Execution, execution_id)
            assert reserved(execution) == Decimal("100")
    finally:
        await _cleanup([user_id])
