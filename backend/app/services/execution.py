from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.action_errors import ActionRetryPolicy, classify_action_error
from app.adapters.hyperliquid import HyperliquidAdapter, OrderOutcome, deterministic_cloid
from app.core.config import Network, settings
from app.core.crypto import EncryptedCredential, crypto
from app.core.logging import get_logger
from app.db.position_ledger_lock import position_ledger_lock
from app.engine.risk import RiskAction, RiskContext, evaluate
from app.engine.sizing import FollowerState, MasterExposure, SizingResult, plan, round_size
from app.models.entities import (
    CopyJob, CopyState, CredentialStatus, EquitySnapshot, Execution, ExecutionState,
    JobState, PositionLedger, RiskHalt, RiskProfile, RiskState, SigningCredential,
    SystemFlag, TradingAccount, User, UserState,
)
from app.services.audit import audit
from app.services.entitlement import entitlement
from app.services.networking import user_network_state

log = get_logger(__name__)
TERMINAL_EXEC = {
    ExecutionState.FILLED,
    ExecutionState.REJECTED,
    ExecutionState.CANCELED,
    ExecutionState.QUARANTINED,
}
_LEDGER_DECIMAL_QUANTUM = Decimal('0.000000000001')


class LedgerApplicationDeferred(RuntimeError):
    """A confirmed fill cannot be safely mapped onto the current local ledger."""


def _persisted_ledger_decimal(value: Decimal) -> Decimal:
    return value.quantize(_LEDGER_DECIMAL_QUANTUM, rounding=ROUND_HALF_UP)


def _effective_master_mark(origin: str, master_mark: Decimal, follower_mark: Decimal, same_network: bool) -> Decimal:
    if origin == 'CLOSE_ALL':
        return follower_mark
    if master_mark <= 0 and same_network:
        return follower_mark
    return master_mark


def _shadow_suppresses_exchange(copy_state: CopyState, origin: str) -> bool:
    return copy_state == CopyState.SHADOW and origin != 'CLOSE_ALL'


def _ambiguity_reduction_plan_safe(plan: SizingResult) -> bool:
    """An ambiguity escape hatch may only reduce the current side, never reverse."""
    return bool(plan.actionable and plan.reduce_only and plan.secondary is None and plan.order_size > 0)


async def _finish_action_rejection(
    db: AsyncSession,
    job: CopyJob,
    *,
    user_id: uuid.UUID,
    network: Network,
    outcome: OrderOutcome,
    leg: str,
    rejected_target: Decimal,
    rejected_real: Decimal,
) -> str:
    """Persist semantic rejection policy without ever blind-resubmitting a CLOID.

    The rejected target and observed position are persisted as immutable evidence
    for retry_policy=NONE. Reconciliation must compare future exchange truth to
    these rejection-time values rather than to PositionLedger, which can change
    independently through observability refreshes.
    """

    decision = classify_action_error(outcome.reason)
    metadata = {
        'class': decision.error_class.value,
        'retry_policy': decision.retry_policy.value,
        'reason': outcome.reason,
        'asset': job.asset,
        'network': network,
        'leg': leg,
        'rejected_target': str(rejected_target),
        'rejected_real': str(rejected_real),
    }
    job.context = {**(job.context or {}), 'last_action_error': metadata}
    await audit(
        db,
        action='COPY_JOB_ACTION_REJECTED',
        subject_id=user_id,
        reason=outcome.reason or 'Order rejected',
        correlation_id=job.correlation_id,
        after=metadata,
    )
    reason = outcome.reason or 'Order rejected'
    if decision.retry_policy is ActionRetryPolicy.RECONCILE:
        reason = f'{reason} [retry policy: fresh reconciliation]'
    return await _finish(db, job, JobState.SKIPPED, reason)


def _blob(cred: SigningCredential) -> EncryptedCredential:
    return EncryptedCredential(
        cred.ciphertext_b64, cred.nonce_b64, cred.wrapped_dek_b64,
        cred.wrap_nonce_b64, cred.key_provider, cred.key_reference, cred.key_version,
    )


def _credential_active(cred: SigningCredential | None) -> bool:
    return bool(
        cred
        and cred.status in {CredentialStatus.ACTIVE, CredentialStatus.EXPIRING}
        and (cred.expires_at is None or cred.expires_at > datetime.now(UTC))
    )


async def claim_job(db: AsyncSession, worker_id: str, job_id: uuid.UUID | None = None) -> CopyJob | None:
    now = datetime.now(UTC)
    query = select(CopyJob).where(
        CopyJob.state.in_([JobState.QUEUED, JobState.RETRYING]),
        (CopyJob.next_attempt_at.is_(None) | (CopyJob.next_attempt_at <= now)),
    )
    if job_id:
        query = query.where(CopyJob.id == job_id)
    job = (await db.execute(query.order_by(CopyJob.created_at).limit(1).with_for_update(skip_locked=True))).scalar_one_or_none()
    if not job:
        return None
    job.state = JobState.PROCESSING
    job.owner = worker_id
    job.locked_until = now + timedelta(seconds=settings.JOB_LEASE_SECONDS)
    job.attempt_count += 1
    await db.commit()
    await db.refresh(job)
    return job


async def release_stale_jobs(db: AsyncSession) -> int:
    now = datetime.now(UTC)
    rows = (await db.execute(select(CopyJob).where(CopyJob.state == JobState.PROCESSING, CopyJob.locked_until < now).with_for_update(skip_locked=True))).scalars().all()
    for job in rows:
        job.state = JobState.RETRYING if job.attempt_count < settings.MAX_JOB_RETRIES else JobState.DEAD
        job.owner = None
        job.locked_until = None
        job.next_attempt_at = now + timedelta(seconds=min(2 ** max(job.attempt_count, 1), 60))
        job.enqueued_at = None
    await db.commit()
    return len(rows)


async def live_trading_allowed(db: AsyncSession, network: Network) -> bool:
    if network != 'mainnet':
        return True
    if not settings.ENABLE_LIVE_TRADING:
        return False
    flag = await db.get(SystemFlag, 'live_trading')
    return bool(flag and flag.enabled)


async def process_job(db: AsyncSession, hl: HyperliquidAdapter, job: CopyJob) -> str:
    expected_owner = job.owner
    async with position_ledger_lock(job.user_id):
        claimed_job = await _renew_job_lease_after_lock(db, job.id, expected_owner)
        if claimed_job is None:
            return JobState.RETRYING.value
        return await _process_job_locked(db, hl, claimed_job)


async def _renew_job_lease_after_lock(
    db: AsyncSession,
    job_id: uuid.UUID,
    expected_owner: str | None,
) -> CopyJob | None:
    """Confirm ownership after a lock wait and renew the full processing lease."""

    job = (
        await db.execute(
            select(CopyJob)
            .where(CopyJob.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if (
        job is None
        or expected_owner is None
        or job.state != JobState.PROCESSING
        or job.owner != expected_owner
    ):
        await db.rollback()
        return None

    job.locked_until = datetime.now(UTC) + timedelta(seconds=settings.JOB_LEASE_SECONDS)
    await db.commit()
    return job


async def _process_job_locked(db: AsyncSession, hl: HyperliquidAdapter, job: CopyJob) -> str:
    user = await db.get(User, job.user_id)
    if not user:
        return await _finish(db, job, JobState.DEAD, 'User no longer exists')

    network_state = await user_network_state(db, user.id)
    network = network_state.network
    ctx = job.context or {}
    job_network = str(ctx.get('follower_network') or network).lower()
    if job_network != network:
        return await _finish(db, job, JobState.SKIPPED, 'Stale job from a previous Hyperliquid network epoch')
    if hl.network != network:
        return await _finish(db, job, JobState.SKIPPED, 'Execution worker adapter does not match the user network')

    account = (await db.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))).scalar_one_or_none()
    risk = (await db.execute(select(RiskProfile).where(RiskProfile.user_id == user.id))).scalar_one_or_none()
    risk_state = (await db.execute(select(RiskState).where(RiskState.user_id == user.id))).scalar_one_or_none()
    if not account or not risk:
        return await _finish(db, job, JobState.SKIPPED, 'Trading account or risk profile missing')
    cred = (await db.execute(select(SigningCredential).where(SigningCredential.trading_account_id == account.id))).scalar_one_or_none()
    ent = await entitlement(db, user)
    equity = (await db.execute(select(EquitySnapshot).where(
        EquitySnapshot.user_id == user.id,
        EquitySnapshot.taken_at >= network_state.started_at,
    ).order_by(EquitySnapshot.taken_at.desc()).limit(1))).scalar_one_or_none()
    ledger = (await db.execute(select(PositionLedger).where(PositionLedger.user_id == user.id, PositionLedger.asset == job.asset))).scalar_one_or_none()
    current = ledger.size if ledger else Decimal(0)

    master_pos = Decimal(str(ctx.get('master_position', '0')))
    master_eq = Decimal(str(ctx.get('master_equity', '0')))
    master_mark = Decimal(str(ctx.get('master_mark_price', ctx.get('mark_price', '0'))))
    if master_eq <= 0:
        return await _retry_or_dead(db, job, 'Master equity unavailable')
    if not equity:
        return await _retry_or_dead(db, job, 'Follower equity not reconciled yet')

    try:
        follower_mids = await hl.mids()
        follower_mark = Decimal(str(follower_mids[job.asset]))
    except Exception:
        return await _retry_or_dead(db, job, 'Follower market price unavailable')
    if follower_mark <= 0:
        return await _retry_or_dead(db, job, 'Follower market price unavailable')
    master_mark = _effective_master_mark(job.origin, master_mark, follower_mark, settings.master_network == network)
    if master_mark <= 0:
        return await _retry_or_dead(db, job, 'Master market price unavailable')

    spec = await hl.asset_spec(job.asset)
    master_leverage = None
    if ctx.get('master_leverage') not in (None, ''):
        try:
            master_leverage = max(1, int(Decimal(str(ctx['master_leverage']))))
        except Exception:
            master_leverage = None
    desired_leverage = None
    if ctx.get('desired_follower_leverage') not in (None, ''):
        try:
            desired_leverage = max(1, int(Decimal(str(ctx['desired_follower_leverage']))))
        except Exception:
            desired_leverage = None
    if desired_leverage is None and master_leverage is not None:
        desired_leverage = max(1, min(master_leverage, int(risk.max_leverage), spec.max_leverage))
    desired_is_cross = bool(ctx.get('desired_follower_is_cross', ctx.get('master_is_cross', True)))
    if spec.only_isolated:
        desired_is_cross = False

    sizing = plan(
        MasterExposure(job.asset, master_pos, master_mark, master_eq),
        FollowerState(str(user.id), equity.account_value, equity.unmanaged_margin, current, risk.multiplier),
        spec,
        min_notional=risk.min_notional,
        follower_mark_price=follower_mark,
    )
    if ledger:
        ledger.target_size = sizing.target_size
    else:
        ledger = PositionLedger(user_id=user.id, asset=job.asset, size=current, target_size=sizing.target_size, mark_price=follower_mark, managed=True)
        db.add(ledger)
    await db.flush()

    ledger.mark_price = follower_mark
    ledgers = (await db.execute(select(PositionLedger).where(PositionLedger.user_id == user.id, PositionLedger.managed.is_(True)))).scalars().all()
    total_exposure = sum((abs(x.size) * max(x.mark_price or Decimal(0), Decimal(0)) for x in ledgers), Decimal(0))
    asset_exposure = abs(current) * follower_mark
    stale = False if job.origin == 'CLOSE_ALL' else equity.taken_at < datetime.now(UTC) - timedelta(seconds=settings.LEDGER_STALE_SECONDS)
    allowed_asset = (not risk.allow_assets or job.asset in risk.allow_assets) and job.asset not in risk.block_assets
    global_pause = bool((await db.get(SystemFlag, 'global_pause')) and (await db.get(SystemFlag, 'global_pause')).enabled)
    emergency_stop = bool((await db.get(SystemFlag, 'emergency_stop')) and (await db.get(SystemFlag, 'emergency_stop')).enabled)
    profile_ctx = RiskContext(
        user_active=user.state == UserState.ACTIVE,
        entitlement_active=bool(ent['entitled']),
        credential_active=_credential_active(cred),
        user_paused=user.copy_state == CopyState.PAUSED,
        global_pause=global_pause,
        emergency_stop=emergency_stop,
        close_only=risk.close_only,
        asset_allowed=allowed_asset,
        drawdown_halt=bool(risk_state and risk_state.state == RiskHalt.DRAWDOWN_HALT),
        daily_loss_halt=bool(risk_state and risk_state.state == RiskHalt.DAILY_LOSS_HALT),
        near_liquidation=bool(risk_state and risk_state.near_liquidation),
        data_stale=stale,
        current_total_exposure=total_exposure,
        current_asset_exposure=asset_exposure,
        free_margin=max(equity.free_margin, Decimal(0)),
        account_equity=max(equity.account_value, Decimal(0)),
        current_leverage=total_exposure / equity.account_value if equity.account_value > 0 else Decimal(999),
        open_positions=len([x for x in ledgers if x.size != 0]), is_new_market=current == 0,
        max_notional_per_trade=risk.max_notional_per_trade, max_total_exposure=risk.max_total_exposure,
        max_asset_exposure=risk.max_asset_exposure, max_leverage=min(risk.max_leverage, Decimal(spec.max_leverage)), max_positions=risk.max_positions,
    )
    decision = evaluate(sizing, profile_ctx)

    if _shadow_suppresses_exchange(user.copy_state, job.origin):
        await audit(db, action='SHADOW_TARGET', subject_id=user.id, reason='Shadow mode: no exchange order', correlation_id=job.correlation_id, after={
            'asset': job.asset, 'target': str(sizing.target_size), 'current': str(current), 'delta': str(sizing.delta),
            'master_network': settings.master_network, 'follower_network': network,
            'master_mark': str(master_mark), 'follower_mark': str(follower_mark),
            'master_leverage': master_leverage, 'desired_follower_leverage': desired_leverage,
        })
        return await _finish(db, job, JobState.DONE, 'Shadow mode')

    leverage_sync_only = bool(ctx.get('leverage_sync_only'))
    if leverage_sync_only:
        if ctx.get('ambiguity_safe_reduction'):
            return await _finish(db, job, JobState.SKIPPED, 'Ambiguity-safe reduction cannot perform leverage-only actions')
        if user.copy_state != CopyState.ACTIVE or not allowed_asset or global_pause or emergency_stop:
            return await _finish(db, job, JobState.SKIPPED, 'Leverage synchronization blocked by copy/risk state')
        if not _credential_active(cred):
            return await _finish(db, job, JobState.SKIPPED, 'Trading credential is unavailable')
        if desired_leverage is None:
            return await _retry_or_dead(db, job, 'Master leverage unavailable')
        if not await live_trading_allowed(db, network):
            return await _finish(db, job, JobState.SKIPPED, 'Mainnet live-trading gate is closed')
        private_key = crypto.decrypt(_blob(cred), user_id=str(user.id), account_id=str(account.id))
        try:
            try:
                response = await hl.update_leverage(
                    account_address=account.account_address,
                    private_key=private_key,
                    asset=job.asset,
                    leverage=desired_leverage,
                    is_cross=desired_is_cross,
                )
            except Exception as exc:
                return await _retry_or_dead(db, job, f'Leverage synchronization failed: {type(exc).__name__}: {exc}')
            await audit(db, action='FOLLOWER_LEVERAGE_SYNCED', subject_id=user.id, correlation_id=job.correlation_id, after={
                'asset': job.asset, 'leverage': desired_leverage, 'margin_mode': 'cross' if desired_is_cross else 'isolated',
                'network': network, 'response': response,
            })
            return await _finish(db, job, JobState.DONE, None)
        finally:
            private_key = ''

    if decision.action in {RiskAction.DENY, RiskAction.SKIP}:
        await audit(db, action='COPY_JOB_BLOCKED', subject_id=user.id, reason=decision.reason, correlation_id=job.correlation_id, after={'asset': job.asset, 'target': str(sizing.target_size), 'current': str(current), 'network': network})
        return await _finish(db, job, JobState.SKIPPED, decision.reason or 'Not actionable')

    if ctx.get('ambiguity_safe_reduction') and not _ambiguity_reduction_plan_safe(decision.plan):
        reason = 'Ambiguity safety fence refused a non-reduce-only or reversal plan'
        await audit(
            db,
            action='AMBIGUITY_REDUCTION_BLOCKED',
            subject_id=user.id,
            reason=reason,
            correlation_id=job.correlation_id,
            after={
                'asset': job.asset,
                'current': str(current),
                'target': str(decision.plan.target_size),
                'reduce_only': decision.plan.reduce_only,
                'has_secondary': decision.plan.secondary is not None,
                'network': network,
            },
        )
        return await _finish(db, job, JobState.SKIPPED, reason)

    if not await live_trading_allowed(db, network):
        return await _finish(db, job, JobState.SKIPPED, 'Mainnet live-trading gate is closed')
    if not cred:
        return await _finish(db, job, JobState.SKIPPED, 'Credential unavailable')

    if not sizing.reduce_only and master_pos != 0 and desired_leverage is None:
        return await _retry_or_dead(db, job, 'Master leverage unavailable; refusing to increase exposure')

    private_key = crypto.decrypt(_blob(cred), user_id=str(user.id), account_id=str(account.id))
    try:
        if desired_leverage is not None and allowed_asset and not ctx.get('ambiguity_safe_reduction'):
            try:
                await hl.update_leverage(
                    account_address=account.account_address,
                    private_key=private_key,
                    asset=job.asset,
                    leverage=desired_leverage,
                    is_cross=desired_is_cross,
                )
            except Exception as exc:
                return await _retry_or_dead(db, job, f'Leverage synchronization failed: {type(exc).__name__}: {exc}')
            await audit(db, action='FOLLOWER_LEVERAGE_SYNCED', subject_id=user.id, correlation_id=job.correlation_id, after={
                'asset': job.asset, 'leverage': desired_leverage,
                'margin_mode': 'cross' if desired_is_cross else 'isolated', 'network': network,
            })

        primary = decision.plan
        primary_kind = 'c' if primary.intent.value == 'reverse' else 'o'
        outcome = await _execute_leg(db, hl, job, user.id, account.account_address, private_key, primary, follower_mark, risk.max_slippage_bps, primary_kind)
        if outcome.state != 'FILLED':
            if outcome.state == 'UNKNOWN':
                return await _retry_or_dead(db, job, outcome.reason or 'Ambiguous execution', ambiguous=True)
            return await _finish_action_rejection(
                db,
                job,
                user_id=user.id,
                network=network,
                outcome=outcome,
                leg='primary',
                rejected_target=primary.target_size,
                rejected_real=ledger.size,
            )
        try:
            await _apply_fill_to_ledger(db, ledger, job, primary_kind, primary, outcome)
        except LedgerApplicationDeferred as exc:
            return await _retry_or_dead(db, job, f'Confirmed fill ledger application deferred: {exc}')
        if primary.secondary:
            outcome2 = await _execute_leg(db, hl, job, user.id, account.account_address, private_key, primary.secondary, follower_mark, risk.max_slippage_bps, 'o')
            if outcome2.state != 'FILLED':
                if outcome2.state == 'UNKNOWN':
                    return await _retry_or_dead(db, job, outcome2.reason or 'Ambiguous reversal open', ambiguous=True)
                return await _finish_action_rejection(
                    db,
                    job,
                    user_id=user.id,
                    network=network,
                    outcome=outcome2,
                    leg='reversal_open',
                    rejected_target=primary.secondary.target_size,
                    rejected_real=ledger.size,
                )
            try:
                await _apply_fill_to_ledger(db, ledger, job, 'o', primary.secondary, outcome2)
            except LedgerApplicationDeferred as exc:
                return await _retry_or_dead(db, job, f'Confirmed reversal fill ledger application deferred: {exc}')
        await audit(db, action='COPY_JOB_EXECUTED', subject_id=user.id, correlation_id=job.correlation_id, after={
            'asset': job.asset, 'target': str(sizing.target_size), 'ledger_size': str(ledger.size),
            'network': network, 'leverage': desired_leverage,
            'margin_mode': 'cross' if desired_is_cross else 'isolated',
        })
        return await _finish(db, job, JobState.DONE, None)
    finally:
        private_key = ''


async def _execute_leg(db: AsyncSession, hl: HyperliquidAdapter, job: CopyJob, user_id, account_address: str, private_key: str, leg: SizingResult, mark: Decimal, slippage_bps: int, kind: str) -> OrderOutcome:
    cloid = deterministic_cloid(str(job.id), kind)
    existing = (await db.execute(select(Execution).where(Execution.copy_job_id == job.id, Execution.attempt_kind == kind))).scalar_one_or_none()
    if existing:
        if existing.state in TERMINAL_EXEC:
            return OrderOutcome(existing.state.value, existing.exchange_oid, existing.filled_size, existing.avg_price, existing.reject_reason, existing.response)
        resolved = await _resolve_cloid(hl, account_address, cloid)
        if resolved.state != 'UNKNOWN':
            await _persist_outcome(db, existing, resolved)
        return resolved

    spec = await hl.asset_spec(job.asset)
    size = round_size(leg.order_size, spec.sz_decimals)
    slip = Decimal(slippage_bps) / Decimal(10_000)
    limit_px = mark * (Decimal(1)+slip if leg.is_buy else Decimal(1)-slip)
    execution = Execution(
        copy_job_id=job.id,
        user_id=user_id,
        attempt_kind=kind,
        cloid=cloid,
        state=ExecutionState.SUBMITTING,
        asset=job.asset,
        is_buy=leg.is_buy,
        requested_size=size,
        reduce_only=leg.reduce_only,
        limit_px=limit_px,
        response={
            'hf007': {
                'ledger_size_before_submit': str(_persisted_ledger_decimal(leg.current_size)),
            }
        },
    )
    db.add(execution)
    await db.commit()
    try:
        outcome = await hl.place_ioc(account_address=account_address, private_key=private_key, asset=job.asset, is_buy=leg.is_buy, size=size, mark_price=mark, slippage_bps=slippage_bps, reduce_only=leg.reduce_only, cloid=cloid)
    except Exception as exc:
        execution.state = ExecutionState.UNKNOWN
        execution.reject_reason = f'ambiguous transport failure: {type(exc).__name__}'
        await db.commit()
        return OrderOutcome('UNKNOWN', reason='Exchange submission result is ambiguous')
    await _persist_outcome(db, execution, outcome)
    return outcome


async def _resolve_cloid(hl: HyperliquidAdapter, account: str, cloid: str) -> OrderOutcome:
    try:
        response = await hl.query_order_by_cloid(account, cloid)
    except Exception:
        return OrderOutcome('UNKNOWN', reason='Could not reconcile cloid')
    envelope_status = str(response.get('status', ''))
    if envelope_status == 'unknownOid':
        return OrderOutcome('UNKNOWN', reason='orderStatus=unknownOid', raw=response)
    if envelope_status != 'order':
        return OrderOutcome('UNKNOWN', reason=f'orderStatus envelope={envelope_status or "unknown"}', raw=response)
    wrapper = response.get('order') or {}
    order = wrapper.get('order') or {}
    status = str(wrapper.get('status', '')).strip()
    oid = str(order.get('oid')) if order.get('oid') is not None else None
    if status == 'filled':
        return OrderOutcome('FILLED', oid, Decimal(str(order.get('origSz') or order.get('sz') or '0')), None, raw=response)
    terminal_cancel = {
        'canceled','marginCanceled','vaultWithdrawalCanceled','openInterestCapCanceled',
        'selfTradeCanceled','reduceOnlyCanceled','siblingFilledCanceled','delistedCanceled',
        'liquidatedCanceled','scheduledCancel','tickRejected','minTradeNtlRejected',
        'perpMarginRejected','reduceOnlyRejected','badAloPxRejected','iocCancelRejected',
        'badTriggerPxRejected','marketOrderNoLiquidityRejected',
        'positionIncreaseAtOpenInterestCapRejected','positionFlipAtOpenInterestCapRejected',
        'tooAggressiveAtOpenInterestCapRejected','openInterestIncreaseRejected',
        'insufficientSpotBalanceRejected','oracleRejected','perpMaxPositionRejected',
    }
    if status == 'rejected':
        return OrderOutcome('REJECTED', oid, reason=status, raw=response)
    if status in terminal_cancel:
        return OrderOutcome('CANCELED', oid, reason=status, raw=response)
    return OrderOutcome('UNKNOWN', oid, reason=f'orderStatus={status or "unknown"}', raw=response)


async def _persist_outcome(db: AsyncSession, execution: Execution, outcome: OrderOutcome) -> None:
    hf007 = dict((execution.response or {}).get('hf007') or {})
    execution.state = ExecutionState(outcome.state)
    execution.exchange_oid = outcome.oid
    execution.filled_size = outcome.filled_size
    execution.avg_price = outcome.avg_price
    execution.reject_reason = outcome.reason
    response = dict(outcome.raw or {})
    if hf007:
        response['hf007'] = hf007
    execution.response = response
    if execution.state in TERMINAL_EXEC:
        execution.resolved_at = datetime.now(UTC)
    await db.commit()


async def _apply_fill_to_ledger(
    db: AsyncSession,
    ledger: PositionLedger,
    job: CopyJob,
    kind: str,
    _leg: SizingResult,
    _outcome: OrderOutcome,
) -> bool:
    """Apply one durable Execution fill to the ledger at most once.

    The exchange outcome is persisted before this function runs. The pre-submit
    ledger position is journaled in Execution.response before the signed action.
    Ledger mutation and the per-Execution applied marker are committed together,
    making a crash after this commit safe to replay.
    """

    execution = (
        await db.execute(
            select(Execution)
            .where(
                Execution.copy_job_id == job.id,
                Execution.attempt_kind == kind,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if execution is None or execution.state != ExecutionState.FILLED:
        raise LedgerApplicationDeferred('durable FILLED execution is unavailable')

    response = dict(execution.response or {})
    hf007 = dict(response.get('hf007') or {})
    if hf007.get('ledger_applied_at'):
        await db.commit()
        return False

    before_raw = hf007.get('ledger_size_before_submit')
    if before_raw in (None, ''):
        if ledger.last_execution_id == execution.id:
            hf007.update({
                'ledger_applied_at': datetime.now(UTC).isoformat(),
                'ledger_apply_mode': 'already_reflected',
                'ledger_size_after': str(_persisted_ledger_decimal(ledger.size)),
                'legacy_metadata': True,
            })
            response['hf007'] = hf007
            execution.response = response
            await db.commit()
            return False
        hf007.update({
            'ledger_apply_deferred_at': datetime.now(UTC).isoformat(),
            'ledger_apply_deferred_reason': 'legacy execution lacks ledger_size_before_submit',
            'ledger_observed': str(_persisted_ledger_decimal(ledger.size)),
        })
        response['hf007'] = hf007
        execution.response = response
        await db.commit()
        raise LedgerApplicationDeferred('legacy execution lacks pre-submit ledger evidence')

    try:
        expected_before = _persisted_ledger_decimal(Decimal(str(before_raw)))
    except Exception as exc:
        raise LedgerApplicationDeferred('invalid pre-submit ledger evidence') from exc

    filled = _persisted_ledger_decimal(execution.filled_size or Decimal(0))
    if filled <= 0:
        raise LedgerApplicationDeferred('FILLED execution has no positive filled_size')
    signed = filled if execution.is_buy else -filled
    expected_after = _persisted_ledger_decimal(expected_before + signed)
    observed = _persisted_ledger_decimal(ledger.size)

    applied = False
    if observed == expected_before:
        ledger.size = expected_after
        applied = True
        mode = 'applied'
    elif observed == expected_after or ledger.last_execution_id == execution.id:
        mode = 'already_reflected'
    else:
        hf007.update({
            'ledger_apply_deferred_at': datetime.now(UTC).isoformat(),
            'ledger_apply_deferred_reason': 'ledger moved outside expected pre/post fill states',
            'ledger_observed': str(observed),
            'ledger_expected_before': str(expected_before),
            'ledger_expected_after': str(expected_after),
        })
        response['hf007'] = hf007
        execution.response = response
        await db.commit()
        raise LedgerApplicationDeferred(
            f'ledger {observed} is neither expected pre-fill {expected_before} nor post-fill {expected_after}'
        )

    if execution.avg_price is not None:
        ledger.mark_price = execution.avg_price
    ledger.last_execution_id = execution.id
    hf007.update({
        'ledger_applied_at': datetime.now(UTC).isoformat(),
        'ledger_apply_mode': mode,
        'ledger_signed_fill': str(signed),
        'ledger_size_before': str(expected_before),
        'ledger_size_after': str(expected_after),
    })
    response['hf007'] = hf007
    execution.response = response
    await db.commit()
    return applied


async def _finish(db: AsyncSession, job: CopyJob, state: JobState, error: str | None) -> str:
    job.state = state
    job.last_error = error
    job.owner = None
    job.locked_until = None
    await db.commit()
    return state.value


async def _retry_or_dead(db: AsyncSession, job: CopyJob, reason: str, ambiguous: bool = False) -> str:
    job.last_error = reason
    job.owner = None
    job.locked_until = None
    if job.attempt_count >= settings.MAX_JOB_RETRIES:
        job.state = JobState.DEAD
    else:
        job.state = JobState.RETRYING
        delay = min(2 ** job.attempt_count, 60) * random.uniform(0.8, 1.2)
        job.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
        job.enqueued_at = None
    await db.commit()
    return job.state.value
