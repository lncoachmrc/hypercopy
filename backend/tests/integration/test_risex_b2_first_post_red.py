"""RED integration contracts for RISEx b2 exactly-once first POST claim."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.db.session import SessionLocal, engine
from app.models.entities import (
    CopyJob,
    Execution,
    ExecutionEpoch,
    ExecutionState,
    JobState,
    CredentialStatus,
    RISExSigningCredential,
    RISExTradingAccount,
    User,
    UserState,
)
from app.security.risex_order_codec import RISExPlaceOrder, build_place_order_action_hash
from app.security.risex_place_order_permit import RISExPreparedPlaceOrderPermit
from app.security.risex_place_order_request import prepare_place_order_request
from app.services import risex_copy_execution, risex_order_preparation


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="requires CI PostgreSQL",
)

ACCOUNT = "0x" + ("11" * 20)
SIGNER = "0x" + ("22" * 20)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_pool_after_test():
    yield
    await engine.dispose()


def _require(name: str):
    value = getattr(risex_copy_execution, name, None)
    assert value is not None, f"RED: expected app.services.risex_copy_execution.{name}"
    return value


def _material(*, execution_id: uuid.UUID, cloid: str, client_order_id: int):
    market = risex_order_preparation.RISExMarketMetadata(
        market_id=1,
        step_size=Decimal("0.000001"),
        step_price=Decimal("0.1"),
        min_order_size=Decimal("0.0001"),
        max_leverage=Decimal("50"),
        mark_price=Decimal("86192.08"),
    )
    intent = risex_order_preparation.RISExOrderIntent(
        symbol="BTC",
        is_buy=True,
        requested_size=Decimal("0.000100"),
        reduce_only=False,
        slippage_bps=25,
        client_order_id=client_order_id,
    )
    order = RISExPlaceOrder(
        market_id=1,
        size_steps=100,
        price_ticks=861_920,
        side=0,
        post_only=False,
        reduce_only=False,
        stp_mode=0,
        order_type=1,
        time_in_force=3,
        client_order_id=client_order_id,
        ttl_units=0,
    )
    plan = risex_order_preparation.RISExIOCPlan(
        order=order,
        requested_size=Decimal("0.000100"),
        limit_price=Decimal("86192.0"),
        market=market,
    )
    permit = RISExPreparedPlaceOrderPermit(
        account_address=ACCOUNT,
        signer_address=SIGNER,
        action_hash=build_place_order_action_hash(order),
        nonce_anchor=7,
        nonce_bitmap_index=13,
        deadline=2_000_000_000,
        _signature=bytes([9]) * 65,
    )
    request = prepare_place_order_request(order=order, permit=permit)
    submission_type = _require("RISExPreparedCopySubmission")
    return submission_type(
        execution_id=execution_id,
        cloid=cloid,
        client_order_id=client_order_id,
        intent=intent,
        plan=plan,
        request=request,
    )


async def _seed() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str]:
    user_id = uuid.uuid4()
    epoch_id = uuid.uuid4()
    job_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    cloid = "0x" + uuid.uuid4().hex

    async with SessionLocal() as db:
        db.add(
            User(
                id=user_id,
                auth_wallet="0x" + uuid.uuid4().hex + "00000000",
                state=UserState.SUSPENDED,
            )
        )
        await db.flush()
        db.add(
            ExecutionEpoch(
                id=epoch_id,
                user_id=user_id,
                provider="risex",
                network="testnet",
                account_address="risex-b2-red",
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
                execution_network="testnet",
                asset="BTC",
                origin="EVENT",
                state=JobState.PROCESSING,
                owner="b2-red-worker",
                attempt_count=1,
                correlation_id=uuid.uuid4().hex,
                context={"follower_network": "testnet", "execution_provider": "risex"},
            )
        )
        db.add(
            Execution(
                id=execution_id,
                copy_job_id=job_id,
                user_id=user_id,
                execution_epoch_id=epoch_id,
                execution_provider="risex",
                execution_network="testnet",
                attempt_kind="o",
                cloid=cloid,
                client_order_id=77,
                nonce_anchor=7,
                nonce_bitmap_index=13,
                state=ExecutionState.SUBMITTING,
                asset="BTC",
                is_buy=True,
                requested_size=Decimal("0.000100"),
                reduce_only=False,
                limit_px=Decimal("86192.0"),
                reserved_exposure_usdc=Decimal("0"),
                response={
                    "risex_4b_bis": {
                        "submission_status": "PRE_POST_COMMITTED",
                    }
                },
            )
        )
        await db.commit()

    return user_id, job_id, execution_id, cloid


async def _cleanup(user_id: uuid.UUID) -> None:
    async with SessionLocal() as db:
        await db.execute(delete(Execution).where(Execution.user_id == user_id))
        await db.execute(delete(CopyJob).where(CopyJob.user_id == user_id))
        await db.execute(delete(ExecutionEpoch).where(ExecutionEpoch.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


@pytest.mark.asyncio
async def test_pre_post_committed_can_be_claimed_exactly_once_under_row_lock() -> None:
    claim = _require("claim_risex_first_post")
    user_id, job_id, execution_id, cloid = await _seed()
    async with SessionLocal() as db:
        user = await db.get(User, user_id)
        job = await db.get(CopyJob, job_id)
        assert user is not None
        assert job is not None
        assert job.execution_epoch_id is not None
        epoch = await db.get(ExecutionEpoch, job.execution_epoch_id)
        assert epoch is not None
        user.active_execution_epoch_id = epoch.id
        account = RISExTradingAccount(
            user_id=user_id,
            account_address=str(epoch.account_address),
            verified_at=datetime.now(UTC),
        )
        db.add(account)
        await db.flush()
        db.add(
            RISExSigningCredential(
                risex_trading_account_id=account.id,
                signer_address="0x" + uuid.uuid4().hex + uuid.uuid4().hex[:8],
                ciphertext_b64="fixture-ciphertext",
                nonce_b64="fixture-nonce",
                wrapped_dek_b64="fixture-wrapped-dek",
                wrap_nonce_b64="fixture-wrap-nonce",
                key_provider="test",
                key_reference="b2-first-post-fixture",
                key_version=1,
                generation=int(epoch.credential_version),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                status=CredentialStatus.ACTIVE,
            )
        )
        await db.commit()

    submission = _material(
        execution_id=execution_id,
        cloid=cloid,
        client_order_id=77,
    )

    async def contender():
        async with SessionLocal() as db:
            job = await db.get(CopyJob, job_id)
            assert job is not None
            return await claim(db, job=job, submission=submission)

    try:
        first, second = await asyncio.gather(contender(), contender())
        claimed = [item for item in (first, second) if item is not None]
        assert len(claimed) == 1

        async with SessionLocal() as db:
            execution = await db.get(Execution, execution_id)
            assert execution is not None
            assert execution.state == ExecutionState.SUBMITTING
            assert (
                (execution.response or {})
                .get("risex_4b_bis", {})
                .get("submission_status")
                == "POST_IN_FLIGHT"
            )
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_mismatched_process_local_submission_never_consumes_first_post_claim() -> None:
    claim = _require("claim_risex_first_post")
    user_id, job_id, execution_id, cloid = await _seed()
    submission = _material(
        execution_id=execution_id,
        cloid=cloid,
        client_order_id=78,
    )

    try:
        async with SessionLocal() as db:
            job = await db.get(CopyJob, job_id)
            assert job is not None
            with pytest.raises(Exception, match="mismatch|identity|client_order_id"):
                await claim(db, job=job, submission=submission)

        async with SessionLocal() as db:
            execution = await db.get(Execution, execution_id)
            assert execution is not None
            assert (
                (execution.response or {})
                .get("risex_4b_bis", {})
                .get("submission_status")
                == "PRE_POST_COMMITTED"
            )
    finally:
        await _cleanup(user_id)
