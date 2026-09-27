"""RED/GREEN integration contracts for provider-independent RISEx 4C pre-fences."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.core.config import settings
from app.db.session import SessionLocal, engine
from app.models.entities import (
    CopyJob,
    Execution,
    ExecutionState,
    JobState,
    User,
    UserState,
)
from app.services import queue, risex_copy_execution, risex_worker_submission


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="requires CI PostgreSQL",
)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_pool_after_test():
    yield
    await engine.dispose()


async def _cleanup(user_id: uuid.UUID) -> None:
    async with SessionLocal() as db:
        await db.execute(delete(Execution).where(Execution.user_id == user_id))
        await db.execute(delete(CopyJob).where(CopyJob.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def _seed_same_asset_case(
    *,
    ambiguous_state: ExecutionState,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    user_id = uuid.uuid4()
    source_job_id = uuid.uuid4()
    btc_job_id = uuid.uuid4()
    eth_job_id = uuid.uuid4()
    wallet = "0x" + uuid.uuid4().hex + "00000000"

    async with SessionLocal() as db:
        db.add(User(id=user_id, auth_wallet=wallet, state=UserState.SUSPENDED))
        await db.flush()

        source_job = CopyJob(
            id=source_job_id,
            user_id=user_id,
            execution_provider="risex",
            execution_network="testnet",
            asset="BTC",
            origin="EVENT",
            state=JobState.RETRYING,
            attempt_count=1,
            correlation_id=uuid.uuid4().hex,
            context={"execution_provider": "risex", "follower_network": "testnet"},
        )
        btc_job = CopyJob(
            id=btc_job_id,
            user_id=user_id,
            execution_provider="risex",
            execution_network="testnet",
            asset="BTC",
            origin="EVENT",
            state=JobState.PROCESSING,
            attempt_count=1,
            correlation_id=uuid.uuid4().hex,
            context={"execution_provider": "risex", "follower_network": "testnet"},
        )
        eth_job = CopyJob(
            id=eth_job_id,
            user_id=user_id,
            execution_provider="risex",
            execution_network="testnet",
            asset="ETH",
            origin="EVENT",
            state=JobState.PROCESSING,
            attempt_count=1,
            correlation_id=uuid.uuid4().hex,
            context={"execution_provider": "risex", "follower_network": "testnet"},
        )
        db.add_all([source_job, btc_job, eth_job])
        await db.flush()

        db.add(
            Execution(
                copy_job_id=source_job_id,
                user_id=user_id,
                execution_provider="risex",
                execution_network="testnet",
                attempt_kind="o",
                cloid="0x" + uuid.uuid4().hex,
                client_order_id=1 + (uuid.uuid4().int % ((1 << 63) - 1)),
                reserved_exposure_usdc=Decimal("50"),
                nonce_anchor=7,
                nonce_bitmap_index=13,
                state=ambiguous_state,
                asset="BTC",
                is_buy=True,
                requested_size=Decimal("0.1"),
                reduce_only=False,
                limit_px=Decimal("100"),
                response={"risex_4b_bis": {"submission_status": "POST_IN_FLIGHT"}},
            )
        )
        await db.commit()

    return user_id, btc_job_id, eth_job_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ambiguous_state",
    [ExecutionState.SUBMITTING, ExecutionState.UNKNOWN],
)
async def test_unresolved_risex_same_asset_blocks_new_job_before_provider_io_integration(
    monkeypatch: pytest.MonkeyPatch,
    ambiguous_state: ExecutionState,
) -> None:
    user_id, btc_job_id, eth_job_id = await _seed_same_asset_case(
        ambiguous_state=ambiguous_state,
    )
    credential_resolution_calls: list[uuid.UUID] = []

    class ReachedCredentialResolution(RuntimeError):
        pass

    async def reached_credential_resolution(_db, job):
        credential_resolution_calls.append(job.id)
        raise ReachedCredentialResolution("different asset passed the same-asset fence")

    monkeypatch.setattr(
        risex_worker_submission,
        "assert_risex_worker_write_allowed",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        risex_worker_submission,
        "resolve_risex_worker_credential",
        reached_credential_resolution,
    )

    class NoPostAdapter:
        calls = 0

        async def place_ioc(self, **_kwargs):
            self.calls += 1
            raise AssertionError("blocked same-asset job must never POST")

    adapter = NoPostAdapter()

    try:
        async with SessionLocal() as db:
            btc_job = await db.get(CopyJob, btc_job_id)
            assert btc_job is not None
            prepared = await risex_worker_submission.prepare_risex_worker_submission(
                db,
                btc_job,
                readiness_assertions={"operatorhub_bypass_disabled": True},
            )
            assert prepared is None
            assert credential_resolution_calls == []

            result = await risex_copy_execution.process_risex_job(
                db,
                adapter,
                btc_job,
                submission=None,
            )
            assert result == JobState.RETRYING.value
            assert adapter.calls == 0

        async with SessionLocal() as db:
            eth_job = await db.get(CopyJob, eth_job_id)
            assert eth_job is not None
            with pytest.raises(
                ReachedCredentialResolution,
                match="different asset passed",
            ):
                await risex_worker_submission.prepare_risex_worker_submission(
                    db,
                    eth_job,
                    readiness_assertions={"operatorhub_bypass_disabled": True},
                )

        assert credential_resolution_calls == [eth_job_id]
    finally:
        await _cleanup(user_id)


async def _seed_expiry_case(
    *,
    ambiguous_state: ExecutionState,
    now: datetime,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    user_id = uuid.uuid4()
    risex_job_id = uuid.uuid4()
    hyperliquid_job_id = uuid.uuid4()
    wallet = "0x" + uuid.uuid4().hex + "00000000"
    old = now - timedelta(seconds=settings.STRATEGY_JOB_MAX_AGE_SECONDS + 60)

    async with SessionLocal() as db:
        db.add(User(id=user_id, auth_wallet=wallet, state=UserState.SUSPENDED))
        await db.flush()

        risex_job = CopyJob(
            id=risex_job_id,
            user_id=user_id,
            execution_provider="risex",
            execution_network="testnet",
            asset="BTC",
            origin="EVENT",
            state=JobState.RETRYING,
            attempt_count=1,
            correlation_id=uuid.uuid4().hex,
            context={"execution_provider": "risex", "follower_network": "testnet"},
            created_at=old,
        )
        hyperliquid_job = CopyJob(
            id=hyperliquid_job_id,
            user_id=user_id,
            execution_provider="hyperliquid",
            execution_network="testnet",
            asset="ETH",
            origin="EVENT",
            state=JobState.RETRYING,
            attempt_count=1,
            correlation_id=uuid.uuid4().hex,
            context={
                "execution_provider": "hyperliquid",
                "follower_network": "testnet",
            },
            created_at=old,
        )
        db.add_all([risex_job, hyperliquid_job])
        await db.flush()

        db.add(
            Execution(
                copy_job_id=risex_job_id,
                user_id=user_id,
                execution_provider="risex",
                execution_network="testnet",
                attempt_kind="o",
                cloid="0x" + uuid.uuid4().hex,
                client_order_id=1 + (uuid.uuid4().int % ((1 << 63) - 1)),
                reserved_exposure_usdc=Decimal("50"),
                state=ambiguous_state,
                asset="BTC",
                is_buy=True,
                requested_size=Decimal("0.1"),
                reduce_only=False,
                limit_px=Decimal("100"),
            )
        )
        await db.commit()

    return user_id, risex_job_id, hyperliquid_job_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ambiguous_state",
    [ExecutionState.SUBMITTING, ExecutionState.UNKNOWN],
)
async def test_risex_ambiguous_job_does_not_expire_but_hyperliquid_still_does_integration(
    ambiguous_state: ExecutionState,
) -> None:
    now = datetime.now(UTC)
    user_id, risex_job_id, hyperliquid_job_id = await _seed_expiry_case(
        ambiguous_state=ambiguous_state,
        now=now,
    )

    try:
        async with SessionLocal() as db:
            expired = await queue.expire_stale_strategy_jobs(db, now=now)
            await db.commit()

        assert expired == 1

        async with SessionLocal() as db:
            risex_job = await db.get(CopyJob, risex_job_id)
            hyperliquid_job = await db.get(CopyJob, hyperliquid_job_id)
            assert risex_job is not None
            assert hyperliquid_job is not None
            assert risex_job.state == JobState.RETRYING
            assert hyperliquid_job.state == JobState.SKIPPED
            assert "Stale strategy job expired" in str(hyperliquid_job.last_error)
    finally:
        await _cleanup(user_id)
