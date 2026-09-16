from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.risex import RISExAdapter
from app.models.entities import CopyJob, JobState
from app.security.risex_place_order_request import RISExPreparedPlaceOrderRequest
from app.services.strategy_intents import (
    StrategyIntentAuthorizationError,
    current_strategy_intent_for_cloid,
)


@dataclass(frozen=True, slots=True)
class RISExPreparedCopySubmission:
    """Process-local signed submission material for one durable RISEx Execution.

    This object is deliberately not JSON-serializable and is never reconstructed
    from CopyJob context. Step 4C remains responsible for fresh provider account
    truth/reconciliation; Step 4B only owns the signed writer boundary.
    """

    cloid: str
    request: RISExPreparedPlaceOrderRequest


async def _finish_without_submission(
    db: AsyncSession,
    job: CopyJob,
    reason: str,
) -> str:
    job.state = JobState.RETRYING
    job.last_error = reason
    job.owner = None
    job.locked_until = None
    job.enqueued_at = None
    await db.commit()
    return JobState.RETRYING.value


async def process_risex_job(
    db: AsyncSession,
    adapter: RISExAdapter,
    job: CopyJob,
    *,
    submission: RISExPreparedCopySubmission | None = None,
) -> str:
    """Execute only an explicitly prepared RISEx submission for this CopyJob.

    The function is intentionally separate from Hyperliquid `_process_job_locked`.
    It reuses the durable latest-intent/destination fence with
    ``execution_provider='risex'`` immediately before the adapter's signed path.
    Signed request material must be process-local; missing material fails closed.
    """

    if job.execution_provider != 'risex' or job.execution_network != 'testnet':
        return await _finish_without_submission(
            db,
            job,
            'RISEx writer received a job outside the RISEx testnet execution epoch',
        )

    if submission is None:
        return await _finish_without_submission(
            db,
            job,
            'RISEx prepared process-local submission is unavailable',
        )

    try:
        await current_strategy_intent_for_cloid(
            cloid=submission.cloid,
            follower_network='testnet',
            asset=job.asset,
            execution_provider='risex',
        )
    except StrategyIntentAuthorizationError as exc:
        job.state = JobState.SKIPPED
        job.last_error = str(exc)
        job.owner = None
        job.locked_until = None
        job.next_attempt_at = None
        job.enqueued_at = None
        await db.commit()
        return JobState.SKIPPED.value

    result: dict[str, Any] = await adapter.place_ioc(
        db=db,
        job=job,
        request=submission.request,
    )
    job.context = {
        **(job.context or {}),
        'risex_submission_result': result,
    }
    job.state = JobState.DONE
    job.last_error = None
    job.owner = None
    job.locked_until = None
    job.next_attempt_at = None
    await db.commit()
    return JobState.DONE.value
