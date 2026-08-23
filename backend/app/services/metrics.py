from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import (
    CopyJob, CredentialStatus, EquitySnapshot, Execution, ExecutionState, Fill,
    JobState, MasterEvent, PositionLedger, ReconciliationRun,
    SigningCredential, WatcherLeaseModel, WorkerHeartbeat,
)
from app.services.networking import user_network_state


PNL_RANGE_CONFIG = {
    '1d': (timedelta(days=1), 5 * 60),
    '7d': (timedelta(days=7), 30 * 60),
    '30d': (timedelta(days=30), 2 * 60 * 60),
    '90d': (timedelta(days=90), 6 * 60 * 60),
    'all': (None, 24 * 60 * 60),
}
SHARPE_WINDOW_DAYS = 90
SHARPE_MIN_OBSERVATIONS = 20


def _first_full_utc_day(start: datetime) -> date:
    normalized = start.astimezone(UTC)
    midnight = datetime(normalized.year, normalized.month, normalized.day, tzinfo=UTC)
    if normalized > midnight:
        midnight += timedelta(days=1)
    return midnight.date()


def _completed_daily_realized_returns(
    equity_points: list[tuple[datetime, float]],
    realized_by_day: dict[date, float],
    *,
    started_at: datetime,
    now: datetime,
) -> list[float]:
    """Build full UTC-day strategy returns without treating cash flows as PnL.

    The numerator is realized closed PnL minus fees. The denominator is the
    first observed account equity for that UTC day. Later deposits/withdrawals
    can change the capital base but never become strategy return themselves.
    Missing opening-equity days are skipped rather than guessed.
    """
    cutoff = max(now - timedelta(days=SHARPE_WINDOW_DAYS), started_at)
    first_day = _first_full_utc_day(cutoff)
    today = now.astimezone(UTC).date()
    last_day = today - timedelta(days=1)
    if first_day > last_day:
        return []

    openings: dict[date, float] = {}
    for taken_at, account_value in equity_points:
        day = taken_at.astimezone(UTC).date()
        if first_day <= day <= last_day and day not in openings and account_value > 0:
            openings[day] = account_value

    returns: list[float] = []
    day = first_day
    while day <= last_day:
        opening_equity = openings.get(day)
        if opening_equity is not None and opening_equity > 0:
            returns.append(realized_by_day.get(day, 0.0) / opening_equity)
        day += timedelta(days=1)
    return returns


def _annualized_sharpe(returns: list[float]) -> tuple[float | None, str]:
    if len(returns) < SHARPE_MIN_OBSERVATIONS:
        return None, 'collecting'
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    if variance <= 0:
        return None, 'zero_variance'
    return mean / math.sqrt(variance) * math.sqrt(365.25), 'ready'


async def pnl_history_for_user(db: AsyncSession, user_id, range_key: str = '1d') -> dict:
    key = range_key.lower()
    if key not in PNL_RANGE_CONFIG:
        raise ValueError('Unsupported PnL range')

    network_state = await user_network_state(db, user_id)
    now = datetime.now(UTC)
    delta, bucket_seconds = PNL_RANGE_CONFIG[key]
    requested_start = now - delta if delta else network_state.started_at
    start = max(requested_start, network_state.started_at)

    net_pnl = func.coalesce(Fill.closed_pnl, 0) - func.coalesce(Fill.fee, 0)
    bucket = func.floor(func.extract('epoch', Fill.ts) / bucket_seconds)
    query = select(
        bucket.label('bucket'),
        func.min(Fill.ts).label('at'),
        func.max(Fill.ts).label('last_at'),
        func.coalesce(func.sum(net_pnl), 0).label('net'),
    ).where(
        Fill.user_id == user_id,
        Fill.ts >= start,
    ).group_by(bucket).order_by(bucket)
    rows = (await db.execute(query)).all()

    latest_equity = (await db.execute(
        select(EquitySnapshot)
        .where(
            EquitySnapshot.user_id == user_id,
            EquitySnapshot.taken_at >= network_state.started_at,
        )
        .order_by(EquitySnapshot.taken_at.desc())
        .limit(1)
    )).scalar_one_or_none()

    baseline = (await db.execute(
        select(EquitySnapshot)
        .where(
            EquitySnapshot.user_id == user_id,
            EquitySnapshot.taken_at >= network_state.started_at,
            EquitySnapshot.taken_at <= start,
        )
        .order_by(EquitySnapshot.taken_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    if baseline is None:
        baseline = (await db.execute(
            select(EquitySnapshot)
            .where(
                EquitySnapshot.user_id == user_id,
                EquitySnapshot.taken_at >= start,
            )
            .order_by(EquitySnapshot.taken_at.asc())
            .limit(1)
        )).scalar_one_or_none()

    total = 0.0
    points = [{'at': start.isoformat(), 'value': 0.0, 'bucket_value': 0.0}]
    for row in rows:
        bucket_value = float(row.net or 0)
        total += bucket_value
        points.append({'at': row.at.isoformat(), 'value': total, 'bucket_value': bucket_value})

    if points[-1]['at'] != now.isoformat():
        points.append({'at': now.isoformat(), 'value': total, 'bucket_value': 0.0})

    start_equity = float(baseline.account_value) if baseline else None
    pnl_pct = (total / start_equity * 100) if start_equity and start_equity > 0 else None

    return {
        'range': key,
        'network': network_state.network,
        'network_started_at': network_state.started_at.isoformat(),
        'pnl_absolute': total,
        'pnl_pct': pnl_pct,
        'start_equity': start_equity,
        'current_equity': float(latest_equity.account_value) if latest_equity else None,
        'last_realized_at': rows[-1].last_at.isoformat() if rows else None,
        'points': points,
        'source': 'realized_net',
    }


async def dashboard_for_user(db: AsyncSession, user_id) -> dict:
    network_state = await user_network_state(db, user_id)
    now = datetime.now(UTC)
    since = max(now - timedelta(days=SHARPE_WINDOW_DAYS), network_state.started_at)
    latest = (await db.execute(select(EquitySnapshot).where(
        EquitySnapshot.user_id == user_id,
        EquitySnapshot.taken_at >= network_state.started_at,
    ).order_by(EquitySnapshot.taken_at.desc()).limit(1))).scalar_one_or_none()
    points = (await db.execute(select(EquitySnapshot).where(
        EquitySnapshot.user_id == user_id,
        EquitySnapshot.taken_at >= since,
    ).order_by(EquitySnapshot.taken_at))).scalars().all()
    values = [float(p.account_value) for p in points]

    # Equity deltas include deposits, withdrawals and account-mode migrations,
    # so they are not trading PnL. Show realized net PnL only for the current
    # Hyperliquid network epoch selected by the user.
    net_pnl = func.coalesce(Fill.closed_pnl, 0) - func.coalesce(Fill.fee, 0)
    realized = (await db.execute(
        select(func.coalesce(func.sum(net_pnl), 0))
        .where(Fill.user_id == user_id, Fill.ts >= since)
    )).scalar_one()
    pnl = float(realized or 0)

    peak = values[0] if values else 0.0
    max_dd = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak * 100)

    # Sharpe follows SPEC's daily-UTC approach but measures strategy PnL rather
    # than raw equity deltas, so deposits/withdrawals are not mistaken for return.
    today_start = datetime(now.year, now.month, now.day, tzinfo=UTC)
    day_bucket = func.date_trunc('day', func.timezone('UTC', Fill.ts))
    daily_rows = (await db.execute(
        select(day_bucket.label('day'), func.coalesce(func.sum(net_pnl), 0).label('net'))
        .where(Fill.user_id == user_id, Fill.ts >= since, Fill.ts < today_start)
        .group_by(day_bucket)
        .order_by(day_bucket)
    )).all()
    realized_by_day = {row.day.date(): float(row.net or 0) for row in daily_rows}
    daily_returns = _completed_daily_realized_returns(
        [(point.taken_at, float(point.account_value)) for point in points],
        realized_by_day,
        started_at=network_state.started_at,
        now=now,
    )
    sharpe, sharpe_status = _annualized_sharpe(daily_returns)

    positions = (await db.execute(select(PositionLedger).where(PositionLedger.user_id == user_id))).scalars().all()
    return {
        'network': network_state.network,
        'network_started_at': network_state.started_at.isoformat(),
        'equity': float(latest.account_value) if latest else None,
        'collateral_balance': float(latest.collateral_balance) if latest else None,
        'unrealized_pnl': float(latest.unrealized_pnl) if latest else None,
        'free_margin': float(latest.free_margin) if latest else None,
        'account_mode': latest.account_mode if latest else None,
        'snapshot_at': latest.taken_at.isoformat() if latest else None,
        'snapshot_age_seconds': max((now - latest.taken_at).total_seconds(), 0) if latest else None,
        'pnl_absolute': pnl,
        'max_drawdown_pct': max_dd,
        'sharpe': sharpe,
        'sharpe_observations': len(daily_returns),
        'sharpe_min_observations': SHARPE_MIN_OBSERVATIONS,
        'sharpe_window_days': SHARPE_WINDOW_DAYS,
        'sharpe_status': sharpe_status,
        'sharpe_method': 'realized_net_daily_utc',
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
