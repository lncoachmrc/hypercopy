from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import (
    CopyJob, CredentialStatus, EquitySnapshot, Execution, ExecutionState, Fill,
    JobState, MasterEvent, PositionLedger, ReconciliationRun,
    SigningCredential, WatcherLeaseModel, WorkerHeartbeat,
)


PNL_RANGE_CONFIG = {
    '1d': (timedelta(days=1), 5 * 60),
    '7d': (timedelta(days=7), 30 * 60),
    '30d': (timedelta(days=30), 2 * 60 * 60),
    '90d': (timedelta(days=90), 6 * 60 * 60),
    'all': (None, 24 * 60 * 60),
}


async def pnl_history_for_user(db: AsyncSession, user_id, range_key: str = '1d') -> dict:
    key = range_key.lower()
    if key not in PNL_RANGE_CONFIG:
        raise ValueError('Unsupported PnL range')

    now = datetime.now(UTC)
    delta, bucket_seconds = PNL_RANGE_CONFIG[key]
    start = now - delta if delta else None

    net_pnl = func.coalesce(Fill.closed_pnl, 0) - func.coalesce(Fill.fee, 0)
    bucket = func.floor(func.extract('epoch', Fill.ts) / bucket_seconds)
    query = select(
        bucket.label('bucket'),
        func.min(Fill.ts).label('at'),
        func.coalesce(func.sum(net_pnl), 0).label('net'),
    ).where(Fill.user_id == user_id)
    if start is not None:
        query = query.where(Fill.ts >= start)
    query = query.group_by(bucket).order_by(bucket)
    rows = (await db.execute(query)).all()

    latest_equity = (await db.execute(
        select(EquitySnapshot)
        .where(EquitySnapshot.user_id == user_id)
        .order_by(EquitySnapshot.taken_at.desc())
        .limit(1)
    )).scalar_one_or_none()

    if start is None:
        baseline = (await db.execute(
            select(EquitySnapshot)
            .where(EquitySnapshot.user_id == user_id)
            .order_by(EquitySnapshot.taken_at.asc())
            .limit(1)
        )).scalar_one_or_none()
        start_at = baseline.taken_at if baseline else (rows[0].at if rows else now)
    else:
        baseline = (await db.execute(
            select(EquitySnapshot)
            .where(EquitySnapshot.user_id == user_id, EquitySnapshot.taken_at <= start)
            .order_by(EquitySnapshot.taken_at.desc())
            .limit(1)
        )).scalar_one_or_none()
        if baseline is None:
            baseline = (await db.execute(
                select(EquitySnapshot)
                .where(EquitySnapshot.user_id == user_id, EquitySnapshot.taken_at >= start)
                .order_by(EquitySnapshot.taken_at.asc())
                .limit(1)
            )).scalar_one_or_none()
        start_at = start

    total = 0.0
    points = [{'at': start_at.isoformat(), 'value': 0.0, 'bucket_value': 0.0}]
    for row in rows:
        bucket_value = float(row.net or 0)
        total += bucket_value
        points.append({'at': row.at.isoformat(), 'value': total, 'bucket_value': bucket_value})

    if not points or points[-1]['at'] != now.isoformat():
        points.append({'at': now.isoformat(), 'value': total, 'bucket_value': 0.0})

    start_equity = float(baseline.account_value) if baseline else None
    pnl_pct = (total / start_equity * 100) if start_equity and start_equity > 0 else None

    return {
        'range': key,
        'pnl_absolute': total,
        'pnl_pct': pnl_pct,
        'start_equity': start_equity,
        'current_equity': float(latest_equity.account_value) if latest_equity else None,
        'points': points,
        'source': 'realized_net',
    }


async def dashboard_for_user(db: AsyncSession, user_id) -> dict:
    since = datetime.now(UTC)-timedelta(days=90)
    latest = (await db.execute(select(EquitySnapshot).where(EquitySnapshot.user_id == user_id).order_by(EquitySnapshot.taken_at.desc()).limit(1))).scalar_one_or_none()
    points = (await db.execute(select(EquitySnapshot).where(EquitySnapshot.user_id == user_id, EquitySnapshot.taken_at >= since).order_by(EquitySnapshot.taken_at))).scalars().all()
    values = [float(p.account_value) for p in points]

    # Equity deltas include deposits, withdrawals and account-mode migrations,
    # so they are not trading PnL. Until funding/unrealized PnL attribution is
    # modelled explicitly, show net realized PnL from HyperCopy fills only.
    realized = (await db.execute(
        select(func.coalesce(func.sum(func.coalesce(Fill.closed_pnl, 0) - func.coalesce(Fill.fee, 0)), 0))
        .where(Fill.user_id == user_id, Fill.ts >= since)
    )).scalar_one()
    pnl = float(realized or 0)

    peak = values[0] if values else 0.0
    max_dd = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak-v)/peak*100)
    daily = {}
    for p in points:
        daily[p.taken_at.date()] = float(p.account_value)
    closes = list(daily.values())
    returns = [(closes[i]/closes[i-1]-1) for i in range(1, len(closes)) if closes[i-1] > 0]
    sharpe = None
    if len(returns) >= 20:
        mean = sum(returns)/len(returns)
        var = sum((r-mean)**2 for r in returns)/(len(returns)-1)
        if var > 0:
            sharpe = mean/math.sqrt(var)*math.sqrt(365.25)
    positions = (await db.execute(select(PositionLedger).where(PositionLedger.user_id == user_id))).scalars().all()
    return {
        'equity': float(latest.account_value) if latest else None,
        'pnl_absolute': pnl,
        'max_drawdown_pct': max_dd,
        'sharpe': sharpe,
        'sharpe_observations': len(returns),
        'positions': len([p for p in positions if p.size != 0]),
    }


async def system_snapshot(db: AsyncSession, rate_snapshot: dict | None = None) -> dict:
    now = datetime.now(UTC)
    queued = (await db.execute(select(func.count()).select_from(CopyJob).where(CopyJob.state.in_([JobState.QUEUED, JobState.RETRYING])))).scalar_one()
    oldest = (await db.execute(select(func.min(CopyJob.created_at)).where(CopyJob.state.in_([JobState.QUEUED, JobState.RETRYING])))).scalar_one_or_none()
    unknown = (await db.execute(select(func.count()).select_from(Execution).where(Execution.state == ExecutionState.UNKNOWN))).scalar_one()
    workers = (await db.execute(select(WorkerHeartbeat).order_by(WorkerHeartbeat.seen_at.desc()).limit(50))).scalars().all()
    recon_fail = (await db.execute(select(func.count()).select_from(ReconciliationRun).where(ReconciliationRun.status == 'FAILED', ReconciliationRun.started_at > now-timedelta(hours=1)))).scalar_one()
    last_master = (await db.execute(select(func.max(MasterEvent.event_ts)))).scalar_one_or_none()
    lease = await db.get(WatcherLeaseModel, 'master-watcher')
    recent_exec = (await db.execute(select(Execution).where(Execution.created_at > now-timedelta(minutes=15)))).scalars().all()
    resolved_latencies = [(x.resolved_at-x.created_at).total_seconds()*1000 for x in recent_exec if x.resolved_at]
    rejected = len([x for x in recent_exec if x.state in {ExecutionState.REJECTED, ExecutionState.CANCELED}])
    expiring_7d = (await db.execute(select(func.count()).select_from(SigningCredential).where(SigningCredential.expires_at.is_not(None), SigningCredential.expires_at <= now+timedelta(days=7), SigningCredential.expires_at > now))).scalar_one()
    return {
        'queue_depth': int(queued),
        'oldest_job_age_seconds': (now-oldest).total_seconds() if oldest else 0,
        'unknown_executions': int(unknown),
        'reconciliation_failures_1h': int(recon_fail),
        'watcher_last_event_age_seconds': (now-last_master).total_seconds() if last_master else None,
        'watcher_lease_holder': lease.holder if lease and lease.expires_at > now else None,
        'watcher_lease_expires_at': lease.expires_at.isoformat() if lease else None,
        'execution_latency_ms_avg_15m': (sum(resolved_latencies)/len(resolved_latencies)) if resolved_latencies else 0,
        'execution_reject_rate_15m': (rejected/len(recent_exec)) if recent_exec else 0,
        'credential_expiring_7d': int(expiring_7d),
        'workers': [{'id': w.worker_id, 'service': w.service, 'seen_at': w.seen_at.isoformat(), 'current_job_id': str(w.current_job_id) if w.current_job_id else None} for w in workers],
        'rate_limit': rate_snapshot or {},
    }
