from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass

from redis.asyncio import Redis

from app.adapters.hyperliquid import PositionConfig
from app.core.config import settings

_CACHE_PREFIX = 'hypercopy:master-leverage-cache'
_METRIC_PREFIX = 'hypercopy:metrics:'
_MISSING_PREFIX = 'hypercopy:master-leverage-missing'


@dataclass(frozen=True, slots=True)
class CachedMasterLeverage:
    config: PositionConfig
    age_seconds: float


def _cache_key(asset: str) -> str:
    address = settings.HYPERLIQUID_MASTER_ADDRESS.lower().strip()
    return f'{_CACHE_PREFIX}:{settings.master_network}:{address}:{asset}'


def _missing_key(asset: str) -> str:
    address = settings.HYPERLIQUID_MASTER_ADDRESS.lower().strip()
    return f'{_MISSING_PREFIX}:{settings.master_network}:{address}:{asset}'


def _metric_key(name: str) -> str:
    return f'{_METRIC_PREFIX}{name}'


async def cache_master_configs(
    redis: Redis,
    configs: dict[str, PositionConfig],
    *,
    now: float | None = None,
) -> None:
    """Persist last-known-good master configs for a short cross-process bridge.

    Values are namespaced by master network/address and expire at the same
    fail-closed freshness boundary used by the watcher snapshot stale bridge.
    Redis is an availability aid only; live Hyperliquid state remains
    authoritative and callers must ignore missing/expired/malformed values.
    """

    observed_at = time.time() if now is None else now
    ttl = max(1, int(math.ceil(settings.HL_MASTER_SNAPSHOT_STALE_SECONDS)))
    for asset, config in configs.items():
        payload = json.dumps(
            {
                'leverage': int(config.leverage),
                'is_cross': bool(config.is_cross),
                'observed_at': observed_at,
            },
            separators=(',', ':'),
        )
        await redis.set(_cache_key(asset), payload, ex=ttl)


async def cached_master_config(
    redis: Redis,
    asset: str,
    *,
    now: float | None = None,
) -> CachedMasterLeverage | None:
    current = time.time() if now is None else now
    try:
        raw = await redis.get(_cache_key(asset))
        if not raw:
            await redis.incr(_metric_key('master_leverage_shared_cache_miss_count'))
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        payload = json.loads(raw)
        observed_at = float(payload['observed_at'])
        age = max(current - observed_at, 0.0)
        if age > settings.HL_MASTER_SNAPSHOT_STALE_SECONDS:
            await redis.delete(_cache_key(asset))
            await redis.incr(_metric_key('master_leverage_shared_cache_stale_count'))
            return None
        leverage = max(int(payload['leverage']), 1)
        config = PositionConfig(leverage=leverage, is_cross=bool(payload['is_cross']))
        await redis.incr(_metric_key('master_leverage_shared_cache_hit_count'))
        await redis.set(_metric_key('master_leverage_shared_cache_age_seconds'), f'{age:.6f}')
        return CachedMasterLeverage(config=config, age_seconds=age)
    except Exception:
        try:
            await redis.incr(_metric_key('master_leverage_shared_cache_error_count'))
        except Exception:
            pass
        return None


async def record_master_leverage_missing(
    redis: Redis,
    asset: str,
    *,
    now: float | None = None,
) -> None:
    current = time.time() if now is None else now
    await redis.incr(_metric_key('master_leverage_unavailable_count'))
    await redis.set(_missing_key(asset), f'{current:.6f}', nx=True, ex=86_400)


async def record_master_leverage_available(
    redis: Redis,
    asset: str,
    *,
    now: float | None = None,
) -> float | None:
    current = time.time() if now is None else now
    raw = await redis.get(_missing_key(asset))
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        started_at = float(raw)
    except (TypeError, ValueError):
        await redis.delete(_missing_key(asset))
        return None

    duration = max(current - started_at, 0.0)
    await redis.delete(_missing_key(asset))
    await redis.incr(_metric_key('master_leverage_recovery_count'))
    await redis.set(_metric_key('master_leverage_recovery_last_seconds'), f'{duration:.6f}')

    max_key = _metric_key('master_leverage_recovery_max_seconds')
    previous_raw = await redis.get(max_key)
    try:
        previous = float(previous_raw.decode() if isinstance(previous_raw, bytes) else previous_raw or 0)
    except (TypeError, ValueError):
        previous = 0.0
    if duration > previous:
        await redis.set(max_key, f'{duration:.6f}')

    if duration > settings.MASTER_LEVERAGE_RECOVERY_SLO_SECONDS:
        await redis.incr(_metric_key('master_leverage_recovery_slo_breach_count'))
    return duration


async def master_leverage_metric_snapshot(redis: Redis) -> dict[str, float | int]:
    names = (
        'master_leverage_unavailable_count',
        'master_leverage_shared_cache_hit_count',
        'master_leverage_shared_cache_miss_count',
        'master_leverage_shared_cache_stale_count',
        'master_leverage_shared_cache_error_count',
        'master_leverage_recovery_count',
        'master_leverage_recovery_slo_breach_count',
    )
    gauges = (
        'master_leverage_shared_cache_age_seconds',
        'master_leverage_recovery_last_seconds',
        'master_leverage_recovery_max_seconds',
    )
    out: dict[str, float | int] = {
        'master_leverage_recovery_slo_seconds': float(settings.MASTER_LEVERAGE_RECOVERY_SLO_SECONDS),
    }
    for name in names:
        raw = await redis.get(_metric_key(name))
        out[name] = int(raw or 0)
    for name in gauges:
        raw = await redis.get(_metric_key(name))
        out[name] = float(raw or 0)
    return out
