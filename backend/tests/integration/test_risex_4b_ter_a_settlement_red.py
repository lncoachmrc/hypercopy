"""RED integration contracts for RISEx 4B-ter A settlement correctness."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text

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

_ACCOUNTING_LOCK_KEY = "hypercopy:risex:mainnet:exposure-budget"


@pytest_asyncio.fixture(autouse=True)
async def _dispose_pool_after_test():
    yield
    await engine.dispose()


def _require(name: str):
    value = getattr(risex_copy_execution, name, None)
    assert value is not None, f"RED: expected app.services.risex_copy_execution.{name}"
    return value


async def _seed_execution(*, suffix: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    user_id = uuid.uuid4()
    epoch_id = uuid.uuid4()
    job_id = uuid.uuid4()
    execution_id = uuid.uuid4()
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
        await db.flush()
        db.add(
            Execution(
                id=execution_id,
                copy_job_id=job_id,
                user_id=user_id,
                execution_epoch_id=epoch_id,
                execution_provider="risex",
                execution_network="mainnet",
                attempt_kind="o",
                cloid="0x" + uuid.uuid4().hex,
                client_order_id=424242,
                reserved_exposure_usdc=Decimal("100"),
                state=ExecutionState.SUBMITTING,
                asset="BTC",
                is_buy=True,
                requested_size=Decimal("0.5"),
                reduce_only=False,
                limit_px=Decimal("200"),
                response={"risex_4b_bis": {"submission_status": "PRE_POST_COMMITTED"}},
            )
        )
        await db.commit()
    return user_id, job_id, execution_id


async def _cleanup(user_id: uuid.UUID) -> None:
    async with SessionLocal() as db:
        await db.execute(delete(Execution).where(Execution.user_id == user_id))
        await db.execute(delete(CopyJob).where(CopyJob.user_id == user_id))
        await db.execute(delete(ExecutionEpoch).where(ExecutionEpoch.user_id == user_id))
        await db.commit()


@pytest.mark.asyncio
async def test_prepost_commit_persists_nonce_anchor_and_bitmap_index_durably() -> None:
    persist = _require("persist_risex_pre_post_execution")
    user_id = uuid.uuid4()
    epoch_id = uuid.uuid4()
    job_id = uuid.uuid4()
    wallet = "0x" + uuid.uuid4().hex + "00000000"

    try:
        async with SessionLocal() as db:
            db.add(User(id=user_id, auth_wallet=wallet, state=UserState.SUSPENDED))
            await db.flush()
            db.add(
                ExecutionEpoch(
                    id=epoch_id,
                    user_id=user_id,
                    provider="risex",
                    network="testnet",
                    account_address="risex-nonce-red",
                    credential_version=1,
                    started_at=datetime.now(UTC),
                )
            )
            await db.flush()
            job = CopyJob(
                id=job_id,
                user_id=user_id,
                execution_epoch_id=epoch_id,
                execution_provider="risex",
                execution_network="testnet",
                asset="BTC",
                origin="EVENT",
                state=JobState.PROCESSING,
                owner="worker-nonce-red",
                attempt_count=1,
                correlation_id=uuid.uuid4().hex,
                context={"follower_network": "testnet", "execution_provider": "risex"},
            )
            db.add(job)
            await db.commit()

        async with SessionLocal() as db:
            job = await db.get(CopyJob, job_id)
            assert job is not None
            execution = await persist(
                db,
                job=job,
                cloid="0x" + uuid.uuid4().hex,
                client_order_id=818181,
                requested_size=Decimal("0.0001"),
                limit_px=Decimal("90000"),
                is_buy=True,
                reduce_only=False,
                exposure_reader=lambda: (Decimal("0"), Decimal("0")),
                user_exposure_ceiling=Decimal("25000"),
                total_exposure_ceiling=Decimal("75000"),
                nonce_anchor=7,
                nonce_bitmap_index=13,
            )
            execution_id = execution.id

        # New session simulates recovery after the pre-POST commit.
        async with SessionLocal() as db:
            durable = await db.get(Execution, execution_id)
            assert durable is not None
            assert durable.nonce_anchor == 7
            assert durable.nonce_bitmap_index == 13
            assert durable.state == ExecutionState.SUBMITTING
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_filled_settlement_refreshes_provider_truth_while_shared_lock_is_held() -> None:
    settle = _require("settle_risex_execution_under_accounting_lock")
    user_id, _job_id, execution_id = await _seed_execution(suffix="settle-lock")

    lock_was_held = False

    async def persist_provider_truth(settlement_db, execution) -> None:
        nonlocal lock_was_held
        assert execution.id == execution_id

        async with SessionLocal() as observer:
            acquired = (
                await observer.execute(
                    text(
                        "SELECT pg_try_advisory_xact_lock("
                        "hashtextextended(:lock_key, 0))"
                    ),
                    {"lock_key": _ACCOUNTING_LOCK_KEY},
                )
            ).scalar_one()
            lock_was_held = acquired is False
            if acquired:
                await observer.rollback()

        response = dict(execution.response or {})
        response["provider_truth_refreshed"] = True
        execution.response = response
        await settlement_db.flush()
        return {"provider_truth_persisted": True}

    outcome = risex_copy_execution.RISExSubmissionOutcome(
        definitive=True,
        execution_state=ExecutionState.FILLED,
        reservation_active=False,
        provider_order_id="risex-order-filled",
        filled_quantity=Decimal("0.5"),
    )

    try:
        async with SessionLocal() as db:
            settled = await settle(
                db,
                execution_id=execution_id,
                outcome=outcome,
                persist_provider_truth=persist_provider_truth,
            )
            assert settled.state == ExecutionState.FILLED

        assert lock_was_held is True

        async with SessionLocal() as db:
            durable = await db.get(Execution, execution_id)
            assert durable is not None
            assert durable.state == ExecutionState.FILLED
            assert durable.resolved_at is not None
            assert (durable.response or {}).get("provider_truth_refreshed") is True
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_missing_provider_truth_callback_keeps_reservation_and_nonterminal_state() -> None:
    settle = _require("settle_risex_execution_under_accounting_lock")
    user_id, _job_id, execution_id = await _seed_execution(suffix="settle-missing-truth")

    outcome = risex_copy_execution.RISExSubmissionOutcome(
        definitive=True,
        execution_state=ExecutionState.FILLED,
        reservation_active=False,
        provider_order_id="risex-order-missing-truth",
        filled_quantity=Decimal("0.5"),
    )

    try:
        async with SessionLocal() as db:
            with pytest.raises(RuntimeError, match="provider truth"):
                await settle(
                    db,
                    execution_id=execution_id,
                    outcome=outcome,
                    persist_provider_truth=None,
                )

        async with SessionLocal() as db:
            durable = await db.get(Execution, execution_id)
            assert durable is not None
            assert durable.state == ExecutionState.SUBMITTING
            assert durable.reserved_exposure_usdc == Decimal("100")
            assert durable.resolved_at is None
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_invalid_provider_truth_callback_result_rolls_back_and_keeps_reservation() -> None:
    settle = _require("settle_risex_execution_under_accounting_lock")
    user_id, _job_id, execution_id = await _seed_execution(suffix="settle-invalid-truth")

    async def invalid_provider_truth(settlement_db, execution):
        response = dict(execution.response or {})
        response["must_rollback"] = True
        execution.response = response
        await settlement_db.flush()
        return {}

    outcome = risex_copy_execution.RISExSubmissionOutcome(
        definitive=True,
        execution_state=ExecutionState.FILLED,
        reservation_active=False,
        provider_order_id="risex-order-invalid-truth",
        filled_quantity=Decimal("0.5"),
    )

    try:
        async with SessionLocal() as db:
            with pytest.raises(RuntimeError, match="provider truth"):
                await settle(
                    db,
                    execution_id=execution_id,
                    outcome=outcome,
                    persist_provider_truth=invalid_provider_truth,
                )

        async with SessionLocal() as db:
            durable = await db.get(Execution, execution_id)
            assert durable is not None
            assert durable.state == ExecutionState.SUBMITTING
            assert durable.reserved_exposure_usdc == Decimal("100")
            assert durable.resolved_at is None
            assert (durable.response or {}).get("must_rollback") is None
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_provider_truth_refresh_failure_keeps_reservation_and_nonterminal_state() -> None:
    settle = _require("settle_risex_execution_under_accounting_lock")
    user_id, _job_id, execution_id = await _seed_execution(suffix="settle-fail")

    async def fail_provider_truth(_db, _execution) -> None:
        raise RuntimeError("provider truth unavailable")

    outcome = risex_copy_execution.RISExSubmissionOutcome(
        definitive=True,
        execution_state=ExecutionState.FILLED,
        reservation_active=False,
        provider_order_id="risex-order-ambiguous-locally",
        filled_quantity=Decimal("0.5"),
    )

    try:
        async with SessionLocal() as db:
            with pytest.raises(RuntimeError, match="provider truth unavailable"):
                await settle(
                    db,
                    execution_id=execution_id,
                    outcome=outcome,
                    persist_provider_truth=fail_provider_truth,
                )
            await db.rollback()

        async with SessionLocal() as db:
            durable = await db.get(Execution, execution_id)
            assert durable is not None
            assert durable.state == ExecutionState.SUBMITTING
            assert durable.reserved_exposure_usdc == Decimal("100")
            assert durable.resolved_at is None
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_definitive_no_effect_rejection_settles_under_lock_without_provider_refresh() -> None:
    settle = _require("settle_risex_execution_under_accounting_lock")
    user_id, _job_id, execution_id = await _seed_execution(suffix="settle-reject")

    outcome = risex_copy_execution.RISExSubmissionOutcome(
        definitive=True,
        execution_state=ExecutionState.REJECTED,
        reservation_active=False,
        reason="provider-confirmed no-effect rejection",
    )

    try:
        async with SessionLocal() as db:
            settled = await settle(
                db,
                execution_id=execution_id,
                outcome=outcome,
                persist_provider_truth=None,
            )
            assert settled.state == ExecutionState.REJECTED

        async with SessionLocal() as db:
            durable = await db.get(Execution, execution_id)
            assert durable is not None
            assert durable.state == ExecutionState.REJECTED
            assert durable.resolved_at is not None
            assert durable.reject_reason == "provider-confirmed no-effect rejection"
    finally:
        await _cleanup(user_id)
