from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.entities import SystemFlag


PUBLIC_PERFORMANCE_RANGE_CONFIG = {
    '1d': (timedelta(days=1), 5 * 60),
    '7d': (timedelta(days=7), 30 * 60),
    '30d': (timedelta(days=30), 2 * 60 * 60),
    '90d': (timedelta(days=90), 6 * 60 * 60),
    'all': (None, 24 * 60 * 60),
}


def _checkpoint_slug() -> str:
    if not settings.HYPERLIQUID_MASTER_ADDRESS:
        raise RuntimeError('Master source is not configured')
    return f'master_checkpoint:{settings.master_network}:{settings.HYPERLIQUID_MASTER_ADDRESS.lower()}'


def _range_start(now: datetime, operational_started_at: datetime, range_key: str) -> tuple[datetime, int]:
    delta, bucket_seconds = PUBLIC_PERFORMANCE_RANGE_CONFIG[range_key]
    requested_start = now - delta if delta else operational_started_at
    return max(requested_start, operational_started_at), bucket_seconds


def _pct(net_pnl: float, baseline_equity: float | None) -> float:
    if baseline_equity is None or baseline_equity <= 0:
        return 0.0
    return net_pnl / baseline_equity * 100


async def public_master_performance(db: AsyncSession, range_key: str = 'all') -> dict:
    key = range_key.lower()
    if key not in PUBLIC_PERFORMANCE_RANGE_CONFIG:
        raise ValueError('Unsupported performance range')

    checkpoint = await db.get(SystemFlag, _checkpoint_slug())
    if checkpoint is None:
        raise RuntimeError('Master performance checkpoint is unavailable')

    now = datetime.now(UTC)
    operational_started_at = checkpoint.created_at.astimezone(UTC)
    start, bucket_seconds = _range_start(now, operational_started_at, key)
    network = settings.master_network

    baseline = (await db.execute(
        text("""
            SELECT master_equity
            FROM master_events
            WHERE event_ts >= :operational_started_at
              AND event_ts <= :start
              AND raw->>'_hypercopy_network' = :network
            ORDER BY event_ts DESC
            LIMIT 1
        """),
        {
            'operational_started_at': operational_started_at,
            'start': start,
            'network': network,
        },
    )).scalar_one_or_none()
    if baseline is None:
        baseline = (await db.execute(
            text("""
                SELECT master_equity
                FROM master_events
                WHERE event_ts >= :start
                  AND raw->>'_hypercopy_network' = :network
                ORDER BY event_ts ASC
                LIMIT 1
            """),
            {'start': start, 'network': network},
        )).scalar_one_or_none()

    rows = (await db.execute(
        text("""
            SELECT
                floor(extract(epoch FROM event_ts) / :bucket_seconds) AS bucket,
                min(event_ts) AS at,
                max(event_ts) AS last_at,
                sum(
                    coalesce(nullif(raw->>'closedPnl', '')::numeric, 0)
                    - coalesce(nullif(raw->>'fee', '')::numeric, 0)
                ) AS net
            FROM master_events
            WHERE event_ts >= :start
              AND event_ts <= :now
              AND raw->>'_hypercopy_network' = :network
            GROUP BY bucket
            ORDER BY bucket
        """),
        {
            'bucket_seconds': bucket_seconds,
            'start': start,
            'now': now,
            'network': network,
        },
    )).all()

    baseline_equity = float(baseline) if baseline is not None else None
    running_net = 0.0
    points = [{'at': start.isoformat(), 'pct': 0.0}]
    last_event_at: datetime | None = None

    for row in rows:
        running_net += float(row.net or 0)
        last_event_at = row.last_at
        points.append({
            'at': row.at.astimezone(UTC).isoformat(),
            'pct': round(_pct(running_net, baseline_equity), 6),
        })

    current_pct = round(_pct(running_net, baseline_equity), 6)
    points.append({'at': now.isoformat(), 'pct': current_pct})

    return {
        'range': key,
        'started_at': operational_started_at.isoformat(),
        'range_started_at': start.isoformat(),
        'updated_at': last_event_at.astimezone(UTC).isoformat() if last_event_at else None,
        'current_pct': current_pct,
        'points': points,
        'source': 'realized_net_pct',
        'status': 'ready' if baseline_equity and rows else 'collecting',
    }
