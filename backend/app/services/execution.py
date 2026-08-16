from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.hyperliquid import HyperliquidAdapter, OrderOutcome, deterministic_cloid
from app.core.config import settings
from app.core.crypto import EncryptedCredential, crypto
from app.core.logging import get_logger
from app.engine.risk import RiskAction, RiskContext, evaluate
from app.engine.sizing import FollowerState, MasterExposure, SizingResult, plan, round_size
from app.models.entities import (
    CopyJob, CopyState, CredentialStatus, EquitySnapshot, Execution, ExecutionState,
    JobState, PositionLedger, RiskHalt, RiskProfile, RiskState, SigningCredential,
    Subscription, SystemFlag, TradingAccount, User, UserState,
)
from app.services.audit import audit
from app.services.entitlement import entitlement

log = get_logger(__name__)
TERMINAL_EXEC = {ExecutionState.FILLED, ExecutionState.REJECTED, ExecutionState.CANCELED}


def _blob(cred: SigningCredential) -> EncryptedCredential:
    return EncryptedCredential(
        cred.ciphertext_b64, cred.nonce_b64, cred.wrapped_dek_b64,
        cred.wrap_nonce_b64, cred.key_provider, cred.key_reference, cred.key_version,
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


async def live_trading_allowed(db: AsyncSession) -> bool:
    # Testnet execution is allowed when a user is ACTIVE; mainnet follower
    # execution additionally requires both environment and DB gates.
    if settings.follower_network != 'mainnet':
        return True
    if not settings.ENABLE_LIVE_TRADING:
        return False
    flag = await db.get(SystemFlag, 'live_trading')
    return bool(flag and flag.enabled)


async def process_job(db: AsyncSession, hl: HyperliquidAdapter, job: CopyJob) -> str:
    user = await db.get(User, job.user_id)
    if not user:
        return await _finish(db, job, JobState.DEAD, 'User no longer exists')
    account = (await db.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))).scalar_one_or_none()
    risk = (await db.execute(select(RiskProfile).where(RiskProfile.user_id == user.id))).scalar_one_or_none()
    risk_state = (await db.execute(select(RiskState).where(RiskState.user_id == user.id))).scalar_one_or_none()
    if not account or not risk:
        return await _finish(db, job, JobState.SKIPPED, 'Trading account or risk profile missing')
    cred = (await db.execute(select(SigningCredential).where(SigningCredential.trading_account_id == account.id))).scalar_one_or_none()
    ent = await entitlement(db, user)
    equity = (await db.execute(select(EquitySnapshot).where(EquitySnapshot.user_id == user.id).order_by(EquitySnapshot.taken_at.desc()).limit(1))).scalar_one_or_none()
    ledger = (await db.execute(select(PositionLedger).where(PositionLedger.user_id == user.id, PositionLedger.asset == job.asset))).scalar_one_or_none()
    current = ledger.size if ledger else Decimal(0)

    ctx = job.context or {}
    master_pos = Decimal(str(ctx.get('master_position', '0')))
    master_eq = Decimal(str(ctx.get('master_equity', '0')))
    master_mark = Decimal(str(ctx.get('master_mark_price', ctx.get('mark_price', '0'))))
    if master_eq <= 0:
        return await _retry_or_dead(db, job, 'Master equity unavailable')
    if not equity:
        return await _retry_or_dead(db, job, 'Follower equity not reconciled yet')

    # Never use a mainnet source price as an executable testnet price. Read the
    # destination market directly and use it for target units, risk valuation,
    # minimum-notional checks and any eventual IOC order.
    try:
        follower_mids = await hl.mids()
        follower_mark = Decimal(str(follower_mids[job.asset]))
    except Exception:
        return await _retry_or_dead(db, job, 'Follower market price unavailable')
    if follower_mark <= 0:
        return await _retry_or_dead(db, job, 'Follower market price unavailable')
    if master_mark <= 0:
        if settings.master_network == settings.follower_network:
            master_mark = follower_mark
        else:
            return await _retry_or_dead(db, job, 'Master market price unavailable')

    spec = await hl.asset_spec(job.asset)
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
    stale = equity.taken_at < datetime.now(UTC) - timedelta(seconds=settings.LEDGER_STALE_SECONDS)
    allowed_asset = (not risk.allow_assets or job.asset in risk.allow_assets) and job.asset not in risk.block_assets
    profile_ctx = RiskContext(
        user_active=user.state == UserState.ACTIVE,
        entitlement_active=bool(ent['entitled']),
        credential_active=bool(cred and cred.status in {CredentialStatus.ACTIVE, CredentialStatus.EXPIRING} and (cred.expires_at is None or cred.expires_at > datetime.now(UTC))),
        user_paused=user.copy_state == CopyState.PAUSED,
        global_pause=bool((await db.get(SystemFlag, 'global_pause')) and (await db.get(SystemFlag, 'global_pause')).enabled),
        emergency_stop=bool((await db.get(SystemFlag, 'emergency_stop')) and (await db.get(SystemFlag, 'emergency_stop')).enabled),
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
    if decision.action in {RiskAction.DENY, RiskAction.SKIP}:
        await audit(db, action='COPY_JOB_BLOCKED', subject_id=user.id, reason=decision.reason, correlation_id=job.correlation_id, after={'asset': job.asset, 'target': str(sizing.target_size), 'current': str(current)})
        return await _finish(db, job, JobState.SKIPPED, decision.reason or 'Not actionable')

    if user.copy_state == CopyState.SHADOW:
        await audit(db, action='SHADOW_TARGET', subject_id=user.id, reason='Shadow mode: no exchange order', correlation_id=job.correlation_id, after={
            'asset': job.asset,
            'target': str(sizing.target_size),
            'current': str(current),
            'delta': str(sizing.delta),
            'master_network': settings.master_network,
            'follower_network': settings.follower_network,
            'master_mark': str(master_mark),
            'follower_mark': str(follower_mark),
        })
        return await _finish(db, job, JobState.DONE, 'Shadow mode')
    if not await live_trading_allowed(db):
        return await _finish(db, job, JobState.SKIPPED, 'Mainnet live-trading gate is closed')

    if not cred:
        return await _finish(db, job, JobState.SKIPPED, 'Credential unavailable')

    private_key = crypto.decrypt(_blob(cred), user_id=str(user.id), account_id=str(account.id))
    try:
        primary = decision.plan
        outcome = await _execute_leg(db, hl, job, user.id, account.account_address, private_key, primary, follower_mark, risk.max_slippage_bps, 'c' if primary.intent.value == 'reverse' else 'o')
        if outcome.state != 'FILLED':
            if outcome.state == 'UNKNOWN':
                return await _retry_or_dead(db, job, outcome.reason or 'Ambiguous execution', ambiguous=True)
            return await _finish(db, job, JobState.SKIPPED, outcome.reason or 'Order rejected')
        await _apply_fill_to_ledger(db, ledger, primary, outcome)
        if primary.secondary:
            outcome2 = await _execute_leg(db, hl, job, user.id, account.account_address, private_key, primary.secondary, follower_mark, risk.max_slippage_bps, 'o')
            if outcome2.state != 'FILLED':
                if outcome2.state == 'UNKNOWN':
                    return await _retry_or_dead(db, job, outcome2.reason or 'Ambiguous reversal open', ambiguous=True)
                return await _finish(db, job, JobState.SKIPPED, outcome2.reason or 'Reversal open rejected')
            await _apply_fill_to_ledger(db, ledger, primary.secondary, outcome2)
        await audit(db, action='COPY_JOB_EXECUTED', subject_id=user.id, correlation_id=job.correlation_id, after={'asset': job.asset, 'target': str(sizing.target_size), 'ledger_size': str(ledger.size), 'network': settings.follower_network})
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
    execution = Execution(copy_job_id=job.id, user_id=user_id, attempt_kind=kind, cloid=cloid, state=ExecutionState.SUBMITTING, asset=job.asset, is_buy=leg.is_buy, requested_size=size, reduce_only=leg.reduce_only, limit_px=limit_px)
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
    execution.state = ExecutionState(outcome.state)
    execution.exchange_oid = outcome.oid
    execution.filled_size = outcome.filled_size
    execution.avg_price = outcome.avg_price
    execution.reject_reason = outcome.reason
    execution.response = outcome.raw or {}
    if execution.state in TERMINAL_EXEC:
        execution.resolved_at = datetime.now(UTC)
    await db.commit()


async def _apply_fill_to_ledger(db: AsyncSession, ledger: PositionLedger, leg: SizingResult, outcome: OrderOutcome) -> None:
    filled = outcome.filled_size or leg.order_size
    signed = filled if leg.is_buy else -filled
    ledger.size += signed
    if outcome.avg_price is not None:
        ledger.mark_price = outcome.avg_price
    await db.commit()


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
