"""PostgreSQL regression for HF-009 expired persistent state validation."""

import os
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio

from app.db.session import SessionLocal, engine
from app.models.entities import (
    CopyJob,
    CopyState,
    Execution,
    ExecutionState,
    JobState,
    User,
    UserState,
)

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="requires CI PostgreSQL",
)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_pool_after_test():
    yield
    await engine.dispose()


@pytest.mark.asyncio
async def test_expired_persistent_state_loads_prior_value_before_transition_guard():
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()

    async with SessionLocal() as db:
        db.add(
            User(
                id=user_id,
                auth_wallet="0x" + uuid.uuid4().hex + "00000000",
                state=UserState.ACTIVE,
                copy_state=CopyState.SHADOW,
            )
        )
        job = CopyJob(
            id=job_id,
            user_id=user_id,
            asset="BTC",
            origin="RECONCILE",
            state=JobState.DONE,
            correlation_id=uuid.uuid4().hex,
            context={},
        )
        execution = Execution(
            copy_job_id=job_id,
            user_id=user_id,
            cloid="0x" + uuid.uuid4().hex,
            state=ExecutionState.FILLED,
            asset="BTC",
            is_buy=True,
            requested_size=Decimal("0.100"),
            reduce_only=False,
            limit_px=Decimal("100"),
            filled_size=Decimal("0.100"),
        )
        db.add(job)
        db.add(execution)
        await db.commit()

        def assert_expired_guards(sync_session):
            # Reproduce an expired/unloaded persistent scalar. With active
            # history the assignment loads the durable prior state before the
            # listener validates the transition instead of seeing NO_VALUE.
            sync_session.expire(job, ["state"])
            with pytest.raises(ValueError, match="Invalid job transition DONE -> PROCESSING"):
                job.state = JobState.PROCESSING
            assert job.state is JobState.DONE

            sync_session.expire(execution, ["state"])
            with pytest.raises(ValueError, match="Invalid execution transition FILLED -> UNKNOWN"):
                execution.state = ExecutionState.UNKNOWN
            assert execution.state is ExecutionState.FILLED

        await db.run_sync(assert_expired_guards)
