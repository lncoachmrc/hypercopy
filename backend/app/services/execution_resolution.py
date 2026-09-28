from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.hyperliquid import HyperliquidAdapter, OrderOutcome
from app.db.position_ledger_lock import position_ledger_lock
from app.models.entities import (
    CopyJob,
    Execution,
    ExecutionState,
    JobState,
    PositionLedger,
    SystemIncident,
    TradingAccount,
)
from app.services.audit import audit
from app.services.execution import _persist_outcome, _resolve_cloid
from app.services.networking import user_network_state

# An IOC submitted by TRAXION carries a 15s expiresAfter window. Ten minutes
# gives multiple reconciliation cycles to recover a delayed read while placing
# a hard upper bound on silent fencing. This is a TRAXION safety SLA, not an
# exchange timeout.
UNKNOWN_EXECUTION_SLA_SECONDS = 600

_ACTIVE_JOB_STATES = {JobState.QUEUED, JobState.PROCESSING, JobState.RETRYING}
_TERMINAL_JOB_STATES = {JobState.DONE, JobState.SKIPPED, JobState.DEAD}


def _position_size(state: dict, asset: str) -> Decimal:
    for row in state.get('assetPositions', []):
        position = row.get('position', row)
        if str(position.get('coin') or '') == asset:
            return Decimal(str(position.get('szi', '0') or '0'))
    return Decimal(0)


async def _sync_asset_ledger_from_exchange_state(
    db: AsyncSession,
    execution: Execution,
    state: dict,
    *,
    verified_at: datetime,
    mark_price: Decimal | None = None,
) -> Decimal:
    """Persist authoritative follower position before removing an ambiguity fence."""

    exchange_position = _position_size(state, execution.asset)
    ledger = (
        await db.execute(
            select(PositionLedger)
            .where(
                PositionLedger.user_id == execution.user_id,
                PositionLedger.asset == execution.asset,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if ledger is None:
        ledger = PositionLedger(
            user_id=execution.user_id,
            asset=execution.asset,
            size=exchange_position,
            target_size=exchange_position,
            mark_price=mark_price or Decimal(0),
            managed=True,
            last_execution_id=execution.id,
            exchange_verified_at=verified_at,
        )
        db.add(ledger)
    else:
        ledger.size = exchange_position
        ledger.managed = True
        ledger.last_execution_id = execution.id
        ledger.exchange_verified_at = verified_at
        if mark_price is not None:
            ledger.mark_price = mark_price
    await db.flush()
    return exchange_position


def _matching_fill_summary(fills: list[dict], oid: str | None) -> tuple[Decimal, Decimal | None, int]:
    if not oid:
        return Decimal(0), None, 0
    matched = [fill for fill in fills if str(fill.get('oid', '')) == str(oid)]
    if not matched:
        return Decimal(0), None, 0
    total = sum((Decimal(str(fill.get('sz', '0') or '0')) for fill in matched), Decimal(0))
    if total <= 0:
        return Decimal(0), None, len(matched)
    notional = sum(
        (
            Decimal(str(fill.get('sz', '0') or '0'))
            * Decimal(str(fill.get('px', '0') or '0'))
            for fill in matched
        ),
        Decimal(0),
    )
    return total, (notional / total if notional > 0 else None), len(matched)


async def _recent_fills(
    hl: HyperliquidAdapter,
    account_address: str,
    execution: Execution,
) -> list[dict]:
    start_ms = max(int(execution.created_at.timestamp() * 1000) - 60_000, 0)
    try:
        return await hl.user_fills_by_time(account_address, start_ms)
    except Exception:
        return []


async def _ensure_aged_incident(
    db: AsyncSession,
    execution: Execution,
    job: CopyJob,
    *,
    reason: str,
) -> uuid.UUID:
    response = dict(execution.response or {})
    hf001 = dict(response.get('hf001') or {})
    existing_id = hf001.get('incident_id')
    if existing_id:
        try:
            return uuid.UUID(str(existing_id))
        except (TypeError, ValueError):
            pass

    incident = SystemIncident(
        severity='HIGH',
        code='EXECUTION_UNKNOWN_AGED',
        message='Execution ambiguity exceeded the TRAXION recovery SLA',
        context={
            'execution_id': str(execution.id),
            'copy_job_id': str(job.id),
            'user_id': str(execution.user_id),
            'asset': execution.asset,
            'execution_state': execution.state.value,
            'job_state': job.state.value,
            'reason': reason,
        },
    )
    db.add(incident)
    await db.flush()
    hf001['incident_id'] = str(incident.id)
    hf001['aged_alerted_at'] = datetime.now(UTC).isoformat()
    response['hf001'] = hf001
    execution.response = response
    return incident.id


async def _resolve_incident(db: AsyncSession, incident_id: str | None) -> None:
    if not incident_id:
        return
    try:
        incident = await db.get(SystemIncident, uuid.UUID(str(incident_id)))
    except (TypeError, ValueError):
        return
    if incident and incident.resolved_at is None:
        incident.resolved_at = datetime.now(UTC)


async def resolve_ambiguous_executions(
    db: AsyncSession,
    hl: HyperliquidAdapter,
    *,
    user_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Resolve SUBMITTING/UNKNOWN executions independently of CopyJob retries.

    The resolver is read-only toward Hyperliquid: it queries CLOID/order status,
    fill history and current account position. It never signs or resubmits an
    order. Once the SLA expires, only an already-terminal CopyJob may be moved
    to QUARANTINED, and only after current exchange position was observed.
    """

    query = select(Execution.id, Execution.user_id).where(
        Execution.state.in_([ExecutionState.SUBMITTING, ExecutionState.UNKNOWN])
    ).order_by(Execution.created_at)
    if user_id is not None:
        query = query.where(Execution.user_id == user_id)
    candidates = (await db.execute(query)).all()

    result = {'checked': 0, 'resolved': 0, 'quarantined': 0, 'aged': 0}

    for execution_id, candidate_user_id in candidates:
        async with position_ledger_lock(candidate_user_id):
            execution = await db.get(Execution, execution_id)
            if not execution or execution.state not in {ExecutionState.SUBMITTING, ExecutionState.UNKNOWN}:
                continue
            job = await db.get(CopyJob, execution.copy_job_id)
            account = (
                await db.execute(
                    select(TradingAccount).where(TradingAccount.user_id == execution.user_id)
                )
            ).scalar_one_or_none()
            if not job or not account:
                continue

            network_state = await user_network_state(db, execution.user_id)
            if network_state.network != hl.network or execution.created_at < network_state.started_at:
                continue

            result['checked'] += 1
            resolved = await _resolve_cloid(hl, account.account_address, execution.cloid)

            fills: list[dict] = []
            if resolved.oid or execution.exchange_oid:
                fills = await _recent_fills(hl, account.account_address, execution)
                evidence_oid = resolved.oid or execution.exchange_oid
                fill_size, avg_price, matched_count = _matching_fill_summary(fills, evidence_oid)
                if resolved.state == 'FILLED' and fill_size > 0:
                    raw = dict(resolved.raw or {})
                    raw['hf001_fill_evidence'] = {
                        'matched_fills': matched_count,
                        'filled_size': str(fill_size),
                    }
                    resolved = OrderOutcome(
                        'FILLED',
                        evidence_oid,
                        fill_size,
                        avg_price,
                        resolved.reason,
                        raw,
                    )
                elif resolved.state == 'UNKNOWN' and fill_size >= execution.requested_size > 0:
                    resolved = OrderOutcome(
                        'FILLED',
                        evidence_oid,
                        fill_size,
                        avg_price,
                        'Recovered from actual Hyperliquid fills after ambiguous submit',
                        {
                            'order_status': resolved.raw or {},
                            'hf001_fill_evidence': {
                                'matched_fills': matched_count,
                                'filled_size': str(fill_size),
                            },
                        },
                    )

            if resolved.state != 'UNKNOWN':
                execution = await db.get(Execution, execution_id)
                if not execution or execution.state not in {ExecutionState.SUBMITTING, ExecutionState.UNKNOWN}:
                    continue

                # A recovered fill removes the execution fence. Before that can
                # happen, persist an authoritative follower snapshot under the
                # same per-user ledger lock. If the snapshot is unavailable,
                # keep UNKNOWN so network switching remains safely blocked.
                if resolved.state == 'FILLED':
                    try:
                        snapshot = await hl.account_snapshot(account.account_address)
                    except Exception:
                        resolved = OrderOutcome(
                            'UNKNOWN',
                            resolved.oid,
                            resolved.filled_size,
                            resolved.avg_price,
                            'Recovered fill is terminal but follower position snapshot is unavailable',
                            resolved.raw,
                        )
                    else:
                        await _sync_asset_ledger_from_exchange_state(
                            db,
                            execution,
                            snapshot.perp_state,
                            verified_at=datetime.now(UTC),
                            mark_price=resolved.avg_price,
                        )

                if resolved.state != 'UNKNOWN':
                    prior_hf001 = dict((execution.response or {}).get('hf001') or {})
                    prior_incident_id = prior_hf001.get('incident_id')
                    await _persist_outcome(db, execution, resolved)
                    if prior_hf001:
                        execution.response = {**(execution.response or {}), 'hf001': prior_hf001}
                    await _resolve_incident(db, prior_incident_id)
                    await audit(
                        db,
                        action='AMBIGUOUS_EXECUTION_RESOLVED',
                        subject_id=execution.user_id,
                        correlation_id=job.correlation_id,
                        after={
                            'execution_id': str(execution.id),
                            'job_id': str(job.id),
                            'asset': execution.asset,
                            'state': execution.state.value,
                            'exchange_oid': execution.exchange_oid,
                            'network': hl.network,
                        },
                    )
                    await db.commit()
                    result['resolved'] += 1
                    continue

            age_seconds = max((datetime.now(UTC) - execution.created_at).total_seconds(), 0)
            if age_seconds < UNKNOWN_EXECUTION_SLA_SECONDS:
                continue

            result['aged'] += 1
            incident_id = await _ensure_aged_incident(
                db,
                execution,
                job,
                reason=resolved.reason or 'CLOID remains unresolved',
            )

            # A live retry owner still controls the intent. The resolver may
            # alert on age, but it cannot take ownership or change the fence.
            if job.state in _ACTIVE_JOB_STATES:
                await db.commit()
                continue
            if job.state not in _TERMINAL_JOB_STATES:
                await db.commit()
                continue

            try:
                snapshot = await hl.account_snapshot(account.account_address)
            except Exception:
                await db.commit()
                continue

            if not fills:
                fills = await _recent_fills(hl, account.account_address, execution)
            evidence_oid = resolved.oid or execution.exchange_oid
            fill_size, avg_price, matched_count = _matching_fill_summary(fills, evidence_oid)

            execution = await db.get(Execution, execution_id)
            job = await db.get(CopyJob, execution.copy_job_id) if execution else None
            if (
                not execution
                or not job
                or execution.state not in {ExecutionState.SUBMITTING, ExecutionState.UNKNOWN}
                or job.state not in _TERMINAL_JOB_STATES
            ):
                await db.rollback()
                continue

            exchange_verified_at = datetime.now(UTC)
            exchange_position = await _sync_asset_ledger_from_exchange_state(
                db,
                execution,
                snapshot.perp_state,
                verified_at=exchange_verified_at,
                mark_price=avg_price,
            )
            quarantined_at = datetime.now(UTC)
            prior_state = execution.state.value
            prior_response = dict(execution.response or {})
            hf001 = dict(prior_response.get('hf001') or {})
            hf001.update(
                {
                    'resolution': 'QUARANTINED',
                    'quarantined_at': quarantined_at.isoformat(),
                    'sla_seconds': UNKNOWN_EXECUTION_SLA_SECONDS,
                    'job_state': job.state.value,
                    'order_status_reason': resolved.reason,
                    'exchange_position': str(exchange_position),
                    'exchange_verified_at': exchange_verified_at.isoformat(),
                    'recent_fill_count': len(fills),
                    'matched_fill_count': matched_count,
                    'matched_fill_size': str(fill_size),
                    'matched_fill_avg_price': str(avg_price) if avg_price is not None else None,
                    'incident_id': str(incident_id),
                }
            )
            prior_response['hf001'] = hf001
            execution.response = prior_response
            execution.state = ExecutionState.QUARANTINED
            execution.resolved_at = quarantined_at
            execution.reject_reason = (
                f'Execution quarantined after {UNKNOWN_EXECUTION_SLA_SECONDS}s SLA; '
                'CLOID remained ambiguous and current exchange position was re-verified'
            )

            incident = await db.get(SystemIncident, incident_id)
            if incident:
                incident.code = 'EXECUTION_UNKNOWN_QUARANTINED'
                incident.message = 'Aged ambiguous execution was quarantined after exchange-state verification'
                incident.context = {
                    **(incident.context or {}),
                    'exchange_position': str(exchange_position),
                    'network': hl.network,
                    'quarantined_at': quarantined_at.isoformat(),
                }

            await audit(
                db,
                action='EXECUTION_UNKNOWN_QUARANTINED',
                subject_id=execution.user_id,
                correlation_id=job.correlation_id,
                reason=execution.reject_reason,
                before={
                    'execution_state': prior_state,
                    'job_state': job.state.value,
                },
                after={
                    'execution_id': str(execution.id),
                    'asset': execution.asset,
                    'state': execution.state.value,
                    'exchange_position': str(exchange_position),
                    'network': hl.network,
                    'incident_id': str(incident_id),
                },
            )
            await db.commit()
            result['quarantined'] += 1

    return result
