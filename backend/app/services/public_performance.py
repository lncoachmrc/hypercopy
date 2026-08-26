from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.master_portfolio import master_mainnet_portfolio


PUBLIC_PERFORMANCE_RANGE_CONFIG = {
    '90d': timedelta(days=90),
    '180d': timedelta(days=180),
    '1y': timedelta(days=365),
    'all': None,
}

# One immutable public epoch for the real strategy source.
# 2026-08-26 08:00 CEST = 2026-08-26 06:00 UTC.
PUBLIC_PERFORMANCE_RESET_AT = datetime(2026, 8, 26, 6, 0, 0, tzinfo=UTC)


def _range_start(now: datetime, range_key: str) -> datetime:
    delta = PUBLIC_PERFORMANCE_RANGE_CONFIG[range_key]
    requested = now - delta if delta else PUBLIC_PERFORMANCE_RESET_AT
    return max(requested, PUBLIC_PERFORMANCE_RESET_AT)


def _sections(portfolio: list[Any]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in portfolio:
        if not isinstance(row, list) or len(row) != 2 or not isinstance(row[1], dict):
            continue
        out[str(row[0])] = row[1]
    return out


def _series(section: dict, key: str) -> list[tuple[int, Decimal]]:
    out: list[tuple[int, Decimal]] = []
    for row in section.get(key, []):
        if not isinstance(row, list) or len(row) != 2:
            continue
        try:
            out.append((int(row[0]), Decimal(str(row[1]))))
        except Exception:
            continue
    out.sort(key=lambda item: item[0])
    return out


def _pick_section(sections: dict[str, dict], start: datetime, now: datetime) -> dict:
    elapsed = now - start
    if elapsed <= timedelta(days=1):
        preferred = ('perpDay', 'day', 'perpWeek', 'week')
    elif elapsed <= timedelta(days=7):
        preferred = ('perpWeek', 'week', 'perpMonth', 'month')
    elif elapsed <= timedelta(days=30):
        preferred = ('perpMonth', 'month', 'perpAllTime', 'allTime')
    else:
        preferred = ('perpAllTime', 'allTime', 'perpMonth', 'month')
    for key in preferred:
        section = sections.get(key)
        if section and section.get('pnlHistory'):
            return section
    raise RuntimeError('Hyperliquid master portfolio history is unavailable')


def _baseline(series: list[tuple[int, Decimal]], start_ms: int) -> tuple[int, Decimal] | None:
    if not series:
        return None
    before = [point for point in series if point[0] <= start_ms]
    if before:
        return before[-1]
    return series[0]


def _pct(delta_pnl: Decimal, baseline_equity: Decimal | None) -> float:
    if baseline_equity is None or baseline_equity <= 0:
        return 0.0
    return float(delta_pnl / baseline_equity * Decimal(100))


async def public_master_performance(_db: AsyncSession, range_key: str = 'all') -> dict:
    key = range_key.lower()
    if key not in PUBLIC_PERFORMANCE_RANGE_CONFIG:
        raise ValueError('Unsupported performance range')

    now = datetime.now(UTC)
    start = _range_start(now, key)
    start_ms = int(start.timestamp() * 1000)

    portfolio = await master_mainnet_portfolio()
    section = _pick_section(_sections(portfolio), start, now)
    pnl_history = _series(section, 'pnlHistory')
    value_history = _series(section, 'accountValueHistory')

    baseline_pnl_point = _baseline(pnl_history, start_ms)
    baseline_value_point = _baseline(value_history, start_ms)
    baseline_pnl = baseline_pnl_point[1] if baseline_pnl_point else Decimal(0)
    baseline_equity = baseline_value_point[1] if baseline_value_point else None

    points = [{'at': start.isoformat(), 'pct': 0.0}]
    last_at: datetime | None = None
    current_pct = 0.0

    for ts_ms, pnl_value in pnl_history:
        if ts_ms < start_ms:
            continue
        at = datetime.fromtimestamp(ts_ms / 1000, UTC)
        current_pct = round(_pct(pnl_value - baseline_pnl, baseline_equity), 6)
        points.append({'at': at.isoformat(), 'pct': current_pct})
        last_at = at

    # Keep the line visually current between Hyperliquid portfolio samples.
    points.append({'at': now.isoformat(), 'pct': current_pct})

    return {
        'range': key,
        'started_at': PUBLIC_PERFORMANCE_RESET_AT.isoformat(),
        'range_started_at': start.isoformat(),
        'updated_at': last_at.isoformat() if last_at else None,
        'current_pct': current_pct,
        'points': points,
        'source': 'hyperliquid_mainnet_portfolio_pnl_pct',
        'network': 'mainnet',
        'status': 'ready' if baseline_equity and pnl_history else 'collecting',
    }
