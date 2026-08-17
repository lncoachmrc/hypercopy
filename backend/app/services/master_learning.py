from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from statistics import median


def _d(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or '0'))


def _sign(value: Decimal) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def learn_master_strategy(events) -> dict:
    """Learn stable operational features from persisted master events.

    This is intentionally deterministic. The LLM receives only this aggregate
    profile, never raw credentials or execution authority. The profile becomes
    richer as more master history accumulates.
    """
    rows = sorted(list(events), key=lambda x: x.event_ts)
    if not rows:
        return {
            'version': 1,
            'event_count': 0,
            'asset_count': 0,
            'observed_days': 0.0,
            'median_event_interval_seconds': None,
            'micro_fill_ratio': 0.0,
            'assets': {},
        }

    per_asset = defaultdict(lambda: {
        'fills': 0,
        'openings': 0,
        'closings': 0,
        'scale_ins': 0,
        'scale_outs': 0,
        'reversals': 0,
        'fill_notionals': [],
        'equity_ratios': [],
        'holding_seconds': [],
    })
    open_since: dict[str, datetime] = {}
    intervals = []
    micro = 0

    previous_ts = None
    for event in rows:
        if previous_ts is not None:
            intervals.append(max((event.event_ts - previous_ts).total_seconds(), 0.0))
        previous_ts = event.event_ts

        asset = str(event.asset)
        start = _d(event.start_position)
        after = _d(event.position_after)
        notional = abs(_d(event.size) * _d(event.price))
        equity = _d(event.master_equity)
        ratio = notional / equity if equity > 0 else Decimal(0)
        a = per_asset[asset]
        a['fills'] += 1
        a['fill_notionals'].append(float(notional))
        a['equity_ratios'].append(float(ratio))
        if ratio < Decimal('0.003'):
            micro += 1

        start_sign = _sign(start)
        after_sign = _sign(after)
        if start == 0 and after != 0:
            a['openings'] += 1
            open_since[asset] = event.event_ts
        elif start != 0 and after == 0:
            a['closings'] += 1
            begun = open_since.pop(asset, None)
            if begun:
                a['holding_seconds'].append(max((event.event_ts - begun).total_seconds(), 0.0))
        elif start_sign != 0 and after_sign != 0 and start_sign != after_sign:
            a['reversals'] += 1
            begun = open_since.get(asset)
            if begun:
                a['holding_seconds'].append(max((event.event_ts - begun).total_seconds(), 0.0))
            open_since[asset] = event.event_ts
        elif abs(after) > abs(start):
            a['scale_ins'] += 1
        elif abs(after) < abs(start):
            a['scale_outs'] += 1

    last_ts = rows[-1].event_ts
    for asset, begun in open_since.items():
        per_asset[asset]['holding_seconds'].append(max((last_ts - begun).total_seconds(), 0.0))

    assets = {}
    for asset, a in per_asset.items():
        fills = max(a['fills'], 1)
        avg_hold = sum(a['holding_seconds']) / len(a['holding_seconds']) if a['holding_seconds'] else 0.0
        holding_component = min(avg_hold / (12 * 3600), 1.0)
        churn = min((a['closings'] + a['reversals']) / fills, 1.0)
        scale_component = min(a['scale_ins'] / fills, 1.0)
        persistence = max(0.0, min(1.0, 0.25 + 0.45 * holding_component + 0.20 * (1.0 - churn) + 0.10 * scale_component))
        assets[asset] = {
            'fills': a['fills'],
            'openings': a['openings'],
            'closings': a['closings'],
            'scale_ins': a['scale_ins'],
            'scale_outs': a['scale_outs'],
            'reversals': a['reversals'],
            'avg_fill_notional': round(sum(a['fill_notionals']) / fills, 4),
            'avg_fill_equity_pct': round(sum(a['equity_ratios']) / fills * 100, 4),
            'avg_holding_minutes': round(avg_hold / 60, 2),
            'persistence_score': round(persistence, 4),
        }

    observed_seconds = max((rows[-1].event_ts - rows[0].event_ts).total_seconds(), 0.0)
    return {
        'version': 1,
        'event_count': len(rows),
        'asset_count': len(assets),
        'observed_days': round(observed_seconds / 86400, 3),
        'median_event_interval_seconds': round(median(intervals), 3) if intervals else None,
        'micro_fill_ratio': round(micro / len(rows), 4),
        'assets': assets,
        'learned_at': datetime.now(UTC).isoformat(),
    }
