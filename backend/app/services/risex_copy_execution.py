from __future__ import annotations

import inspect
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.hyperliquid import deterministic_cloid
from app.adapters.risex import RISExAdapter
from app.models.entities import CopyJob, Execution, ExecutionState, JobState
from app.security.risex_place_order_request import RISExPreparedPlaceOrderRequest
from app.services.strategy_intents import (
    StrategyIntentAuthorizationError,
    current_strategy_intent_for_cloid,
)


_RISEX_EXPOSURE_LOCK_KEY = 'hypercopy:risex:mainnet:exposure-budget'
_ACTIVE_RESERVATION_STATES = (ExecutionState.SUBMITTING, ExecutionState.UNKNOWN)


@dataclass(frozen=True, slots=True)
class RISExPreparedCopySubmission:
    """Process-local signed submission material for one durable RISEx Execution."""

    cloid: str
    request: RISExPreparedPlaceOrderRequest


@dataclass(frozen=True, slots=True)
class RISExSubmissionTransition:
    execution_state: ExecutionState
    reservation_active: bool
    job_terminal: bool


@dataclass(frozen=True, slots=True)
class RISExSubmissionOutcome:
    definitive: bool
    execution_state: ExecutionState
    reservation_active: bool
    provider_order_id: str | None = None
    filled_quantity: Decimal | None = None
    reason: str | None = None


def client_order_id_for_job(job_id: uuid.UUID, attempt_kind: str = 'o') -> int:
    """Derive the non-zero RISEx uint64 id from the canonical Hyperliquid CLOID hash."""

    cloid = deterministic_cloid(str(job_id), attempt_kind)
    return 1 + (int(cloid[2:], 16) % ((1 << 64) - 1))


def submission_transition(kind: str) -> RISExSubmissionTransition:
    """Return fail-closed lifecycle semantics for the local submission boundary."""

    if kind == 'CONFIRMED_FILLED':
        return RISExSubmissionTransition(ExecutionState.FILLED, False, True)
    if kind == 'CONFIRMED_REJECTED':
        return RISExSubmissionTransition(ExecutionState.REJECTED, False, True)
    if kind in {'CONFIRMED_CANCELED', 'PRE_SUBMIT_BLOCKED'}:
        return RISExSubmissionTransition(ExecutionState.CANCELED, False, True)
    if kind == 'AMBIGUOUS':
        return RISExSubmissionTransition(ExecutionState.UNKNOWN, True, False)
    # Unknown acknowledgements never consume the reservation.
    return RISExSubmissionTransition(ExecutionState.SUBMITTING, True, False)


def _decimal_or_none(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return parsed


def classify_risex_submission_response(
    *,
    status_code: int,
    payload: Mapping[str, Any] | object,
    requested_size: Decimal,
) -> RISExSubmissionOutcome:
    """Whitelist only provider-confirmed terminal response shapes.

    Anything not matching a reviewed terminal schema remains SUBMITTING with its
    reservation intact. Absence of an error is never treated as terminal proof.
    """

    if isinstance(payload, Mapping):
        order_id = payload.get('order_id')
        filled = _decimal_or_none(payload.get('filled_quantity'))
        requested = _decimal_or_none(requested_size)

        if (
            status_code == 200
            and isinstance(order_id, str)
            and bool(order_id.strip())
            and filled is not None
            and requested is not None
            and Decimal(0) <= filled <= requested
        ):
            if filled > 0:
                return RISExSubmissionOutcome(
                    definitive=True,
                    execution_state=ExecutionState.FILLED,
                    reservation_active=False,
                    provider_order_id=order_id,
                    filled_quantity=filled,
                )
            return RISExSubmissionOutcome(
                definitive=True,
                execution_state=ExecutionState.CANCELED,
                reservation_active=False,
                provider_order_id=order_id,
                filled_quantity=Decimal(0),
                reason='provider-confirmed IOC completed without fill',
            )

        error = payload.get('error')
        if (
            400 <= status_code < 500
            and payload.get('success') is False
            and isinstance(error, Mapping)
            and isinstance(error.get('code'), str)
            and bool(str(error.get('code')).strip())
            and isinstance(error.get('message'), str)
            and bool(str(error.get('message')).strip())
        ):
            return RISExSubmissionOutcome(
                definitive=True,
                execution_state=ExecutionState.REJECTED,
                reservation_active=False,
                reason=f"{error.get('code')}: {error.get('message')}",
            )

    transition = submission_transition('ACKNOWLEDGED')
    return RISExSubmissionOutcome(
        definitive=False,
        execution_state=transition.execution_state,
        reservation_active=transition.reservation_active,
    )


def reserved_exposure_usdc(execution: object) -> Decimal:
    """Return the active durable mainnet reservation represented by an Execution."""

    if (
        getattr(execution, 'execution_provider', None) != 'risex'
        or getattr(execution, 'execution_network', None) != 'mainnet'
        or getattr(execution, 'reduce_only', False)
        or getattr(execution, 'state', None) not in _ACTIVE_RESERVATION_STATES
    ):
        return Decimal(0)
    value = _decimal_or_none(getattr(execution, 'reserved_exposure_usdc', None))
    return value if value is not None and value > 0 else Decimal(0)


async def _acquire_exposure_budget_lock(db: AsyncSession) -> None:
    await db.execute(
        text('SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))'),
        {'lock_key': _RISEX_EXPOSURE_LOCK_KEY},
    )


async def _read_exposure_pair(exposure_reader: Callable[[], Any]) -> tuple[Decimal, Decimal]:
    result = exposure_reader()
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, tuple) or len(result) != 2:
        raise RuntimeError('RISEx exposure state is indeterminate')
    user_exposure = _decimal_or_none(result[0])
    total_exposure = _decimal_or_none(result[1])
    if user_exposure is None or total_exposure is None or user_exposure < 0 or total_exposure < 0:
        raise RuntimeError('RISEx exposure state is indeterminate')
    return user_exposure, total_exposure


async def persist_risex_pre_post_execution(
    db: AsyncSession,
    *,
    job: CopyJob,
    cloid: str,
    client_order_id: int,
    requested_size: Decimal,
    limit_px: Decimal,
    is_buy: bool,
    reduce_only: bool,
    exposure_reader: Callable[[], Any],
    user_exposure_ceiling: Decimal,
    total_exposure_ceiling: Decimal,
    attempt_kind: str = 'o',
) -> Execution:
    """Atomically authorize and persist the pre-POST Execution/reservation."""

    if job.execution_provider != 'risex' or job.execution_epoch_id is None:
        raise RuntimeError('RISEx durable execution requires a bound RISEx execution epoch')
    if type(client_order_id) is not int or not 0 < client_order_id < (1 << 64):
        raise RuntimeError('RISEx client_order_id must be a non-zero uint64')

    requested = _decimal_or_none(requested_size)
    limit = _decimal_or_none(limit_px)
    if requested is None or limit is None or requested <= 0 or limit <= 0:
        raise RuntimeError('RISEx requested size and limit price must be positive')

    if job.execution_network == 'mainnet':
        await _acquire_exposure_budget_lock(db)

    existing = (
        await db.execute(
            select(Execution).where(
                Execution.copy_job_id == job.id,
                Execution.attempt_kind == attempt_kind,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    reservation = Decimal(0)
    if job.execution_network == 'mainnet' and not reduce_only:
        user_exposure, total_exposure = await _read_exposure_pair(exposure_reader)

        active_filters = (
            Execution.execution_provider == 'risex',
            Execution.execution_network == 'mainnet',
            Execution.state.in_(_ACTIVE_RESERVATION_STATES),
            Execution.reduce_only.is_(False),
            Execution.reserved_exposure_usdc.is_not(None),
        )
        aggregate_reserved = (
            await db.execute(
                select(func.coalesce(func.sum(Execution.reserved_exposure_usdc), 0)).where(
                    *active_filters
                )
            )
        ).scalar_one()
        user_reserved = (
            await db.execute(
                select(func.coalesce(func.sum(Execution.reserved_exposure_usdc), 0)).where(
                    *active_filters,
                    Execution.user_id == job.user_id,
                )
            )
        ).scalar_one()

        reservation = requested * limit
        if (
            user_exposure + Decimal(user_reserved) + reservation > user_exposure_ceiling
            or total_exposure + Decimal(aggregate_reserved) + reservation
            > total_exposure_ceiling
        ):
            await db.rollback()
            raise RuntimeError('RISEx exposure reservation would exceed ADR-0006 ceiling')

    execution = Execution(
        copy_job_id=job.id,
        user_id=job.user_id,
        execution_epoch_id=job.execution_epoch_id,
        execution_provider='risex',
        execution_network=job.execution_network,
        attempt_kind=attempt_kind,
        cloid=cloid,
        client_order_id=client_order_id,
        reserved_exposure_usdc=reservation,
        state=ExecutionState.SUBMITTING,
        asset=job.asset,
        is_buy=is_buy,
        requested_size=requested,
        reduce_only=reduce_only,
        limit_px=limit,
        response={
            'risex_4b_bis': {
                'submission_status': 'PRE_POST_COMMITTED',
            }
        },
    )
    db.add(execution)
    await db.commit()
    return execution


async def _defer_for_resolution(
    db: AsyncSession,
    job: CopyJob,
    reason: str,
) -> str:
    job.state = JobState.RETRYING
    job.last_error = reason
    job.owner = None
    job.locked_until = None
    job.enqueued_at = None
    if job.attempt_count > 0:
        job.attempt_count -= 1
    await db.commit()
    return JobState.RETRYING.value


async def _finish_without_submission(
    db: AsyncSession,
    job: CopyJob,
    reason: str,
) -> str:
    return await _defer_for_resolution(db, job, reason)


async def process_risex_job(
    db: AsyncSession,
    adapter: RISExAdapter,
    job: CopyJob,
    *,
    submission: RISExPreparedCopySubmission | None = None,
) -> str:
    """Submit at most once; provider truth remains fenced until terminal evidence."""

    if job.execution_provider != 'risex' or job.execution_network != 'testnet':
        return await _finish_without_submission(
            db,
            job,
            'RISEx writer received a job outside the RISEx testnet execution epoch',
        )

    existing = (
        await db.execute(
            select(Execution).where(
                Execution.copy_job_id == job.id,
                Execution.attempt_kind == 'o',
            )
        )
    ).scalar_one_or_none()

    if existing is not None and existing.state in _ACTIVE_RESERVATION_STATES:
        return await _defer_for_resolution(
            db,
            job,
            'RISEx execution is awaiting provider-truth reconciliation',
        )
    if existing is not None and existing.state == ExecutionState.FILLED:
        job.state = JobState.DONE
        job.owner = None
        job.locked_until = None
        await db.commit()
        return JobState.DONE.value
    if existing is not None and existing.state in {
        ExecutionState.REJECTED,
        ExecutionState.CANCELED,
        ExecutionState.QUARANTINED,
    }:
        job.state = JobState.SKIPPED
        job.owner = None
        job.locked_until = None
        await db.commit()
        return JobState.SKIPPED.value

    if submission is None:
        return await _finish_without_submission(
            db,
            job,
            'RISEx prepared process-local submission is unavailable',
        )
    if existing is None:
        return await _finish_without_submission(
            db,
            job,
            'RISEx durable pre-POST Execution is unavailable',
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
    outcome = classify_risex_submission_response(
        status_code=200,
        payload=result,
        requested_size=existing.requested_size,
    )

    response = dict(existing.response or {})
    response['risex_4b_bis'] = {
        **dict(response.get('risex_4b_bis') or {}),
        'submission_status': 'CONFIRMED' if outcome.definitive else 'ACKNOWLEDGED',
        'provider_response': dict(result),
    }
    existing.response = response

    if not outcome.definitive:
        existing.state = ExecutionState.SUBMITTING
        return await _defer_for_resolution(
            db,
            job,
            'RISEx provider acknowledgement is not terminal provider truth',
        )

    existing.state = outcome.execution_state
    existing.exchange_oid = outcome.provider_order_id
    existing.filled_size = outcome.filled_quantity or Decimal(0)
    existing.reject_reason = outcome.reason
    existing.resolved_at = datetime.now(UTC)

    if outcome.execution_state == ExecutionState.FILLED:
        job.state = JobState.DONE
        job.last_error = None
    else:
        job.state = JobState.SKIPPED
        job.last_error = outcome.reason
    job.owner = None
    job.locked_until = None
    job.next_attempt_at = None
    await db.commit()
    return job.state.value
