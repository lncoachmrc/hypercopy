"""RED contracts for durable RISEx 4B-bis pre-POST persistence."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

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


async def _seed_job(
    *,
    suffix: str,
    network: str = "mainnet",
) -> tuple[uuid.UUID, uuid.UUID]:
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
                network=network,
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
                execution_network=network,
                asset="BTC",
                origin="EVENT",
                state=JobState.PROCESSING,
                owner=f"worker-{suffix}",
                attempt_count=1,
                correlation_id=uuid.uuid4().hex,
                context={"follower_network": network, "execution_provider": "risex"},
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
async def test_partial_unique_index_rejects_duplicate_provider_network_client_order_id():
    user_a, job_a_id = await _seed_job(suffix="dup-a")
    user_b, job_b_id = await _seed_job(suffix="dup-b")
    users = [user_a, user_b]

    try:
        async with SessionLocal() as db:
            job_a = await db.get(CopyJob, job_a_id)
            job_b = await db.get(CopyJob, job_b_id)
            assert job_a is not None and job_b is not None

            db.add(
                Execution(
                    copy_job_id=job_a.id,
                    user_id=job_a.user_id,
                    execution_epoch_id=job_a.execution_epoch_id,
                    execution_provider="risex",
                    execution_network="mainnet",
                    attempt_kind="o",
                    cloid="0x" + uuid.uuid4().hex,
                    client_order_id=777,
                    state=ExecutionState.SUBMITTING,
                    asset="BTC",
                    is_buy=True,
                    requested_size=Decimal("1"),
                    reduce_only=False,
                    limit_px=Decimal("100"),
                    reserved_exposure_usdc=Decimal("100"),
                )
            )
            await db.commit()

        async with SessionLocal() as db:
            job_b = await db.get(CopyJob, job_b_id)
            assert job_b is not None
            db.add(
                Execution(
                    copy_job_id=job_b.id,
                    user_id=job_b.user_id,
                    execution_epoch_id=job_b.execution_epoch_id,
                    execution_provider="risex",
                    execution_network="mainnet",
                    attempt_kind="o",
                    cloid="0x" + uuid.uuid4().hex,
                    client_order_id=777,
                    state=ExecutionState.SUBMITTING,
                    asset="BTC",
                    is_buy=True,
                    requested_size=Decimal("1"),
                    reduce_only=False,
                    limit_px=Decimal("100"),
                    reserved_exposure_usdc=Decimal("100"),
                )
            )
            with pytest.raises(IntegrityError):
                await db.commit()
            await db.rollback()
    finally:
        await _cleanup(users)


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
async def test_simulated_crash_after_commit_before_post_keeps_reservation_and_durable_client_order_id():
    persist = _require("persist_risex_pre_post_execution")
    user_id, job_id = await _seed_job(suffix="crash")

    async def exposure_reader():
        return Decimal("0"), Decimal("0")

    class SimulatedCrash(RuntimeError):
        pass

    execution_id = None
    try:
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
            # Crash point: durable commit succeeded; no provider POST has occurred.
            raise SimulatedCrash("worker died after commit and before POST")
        except SimulatedCrash:
            pass

        assert execution_id is not None
        async with SessionLocal() as db:
            execution = await db.get(Execution, execution_id)
            assert execution is not None
            assert execution.state == ExecutionState.SUBMITTING
            assert execution.client_order_id == Decimal("424242")
            assert execution.reserved_exposure_usdc == Decimal("100")

            by_job = (
                await db.execute(
                    select(Execution).where(
                        Execution.copy_job_id == job_id,
                        Execution.attempt_kind == "o",
                    )
                )
            ).scalar_one()
            assert by_job.client_order_id == Decimal("424242")
            assert by_job.reserved_exposure_usdc == Decimal("100")
    finally:
        await _cleanup([user_id])


@pytest.mark.asyncio
async def test_ambiguous_existing_execution_never_causes_second_post_and_keeps_reservation(
    monkeypatch: pytest.MonkeyPatch,
):
    user_id, job_id = await _seed_job(suffix="ambiguous", network="testnet")
    cloid = "0x" + uuid.uuid4().hex

    class CountingAdapter:
        def __init__(self):
            self.calls = 0

        async def place_ioc(self, **_kwargs):
            self.calls += 1
            return {
                "order_id": "should-not-exist",
                "filled_quantity": "1",
            }

    async def allow_latest_intent(**_kwargs):
        return SimpleNamespace()

    try:
        async with SessionLocal() as db:
            job = await db.get(CopyJob, job_id)
            assert job is not None
            execution = Execution(
                    copy_job_id=job.id,
                    user_id=job.user_id,
                    execution_epoch_id=job.execution_epoch_id,
                    execution_provider="risex",
                    execution_network="testnet",
                    attempt_kind="o",
                    cloid=cloid,
                    client_order_id=999,
                    state=ExecutionState.UNKNOWN,
                    asset="BTC",
                    is_buy=True,
                    requested_size=Decimal("1"),
                    reduce_only=False,
                    limit_px=Decimal("100"),
                    reserved_exposure_usdc=Decimal("100"),
                )
            db.add(execution)
            await db.flush()
            execution_id = execution.id
            await db.commit()

        monkeypatch.setattr(
            risex_copy_execution,
            "current_strategy_intent_for_cloid",
            allow_latest_intent,
        )

        adapter = CountingAdapter()
        submission = risex_copy_execution.RISExPreparedCopySubmission(
            execution_id=execution_id,
            cloid=cloid,
            client_order_id=999,
            intent=object(),  # UNKNOWN recovery must stop before process-local material is inspected.
            plan=object(),
            request=object(),
        )

        async with SessionLocal() as db:
            job = await db.get(CopyJob, job_id)
            result = await risex_copy_execution.process_risex_job(
                db,
                adapter,
                job,
                submission=submission,
            )

        assert result == JobState.RETRYING.value
        assert adapter.calls == 0

        async with SessionLocal() as db:
            execution = (
                await db.execute(
                    select(Execution).where(
                        Execution.copy_job_id == job_id,
                        Execution.attempt_kind == "o",
                    )
                )
            ).scalar_one()
            assert execution.state == ExecutionState.UNKNOWN
            assert execution.reserved_exposure_usdc == Decimal("100")
    finally:
        await _cleanup([user_id])
