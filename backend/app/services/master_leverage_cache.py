from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from decimal import Decimal

from redis.asyncio import Redis

from app.adapters.hyperliquid import PositionConfig
from app.core.config import settings

_CACHE_PREFIX = 'hypercopy:master-leverage-cache'
_METRIC_PREFIX = 'hypercopy:metrics:'
_MISSING_PREFIX = 'hypercopy:master-leverage-missing'
_ATOMIC_MISSING_SCRIPT = """
local started = redis.call('ZSCORE', KEYS[1], ARGV[1])
local created = 0
if not started then
  redis.call('ZADD', KEYS[1], ARGV[2], ARGV[1])
  created = 1
end
local latest = redis.call('ZSCORE', KEYS[2], ARGV[1])
if not latest or tonumber(ARGV[2]) > tonumber(latest) then
  redis.call('ZADD', KEYS[2], ARGV[2], ARGV[1])
end
return created
"""
_ATOMIC_REPAIR_SCRIPT = """
local started = redis.call('ZSCORE', KEYS[1], ARGV[1])
local latest = redis.call('ZSCORE', KEYS[2], ARGV[1])
if not started or not latest then
  return false
end
if tonumber(ARGV[2]) < tonumber(latest) then
  return false
end
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[1])
return started
"""


@dataclass(frozen=True, slots=True)
class CachedMasterLeverage:
    config: PositionConfig
    master_equity: Decimal
    age_seconds: float


def _master_namespace() -> str:
    address = settings.HYPERLIQUID_MASTER_ADDRESS.lower().strip()
    return f'{settings.master_network}:{address}'


def _cache_key(asset: str) -> str:
    return f'{_CACHE_PREFIX}:{_master_namespace()}:{asset}'


def _missing_started_key() -> str:
    return f'{_MISSING_PREFIX}:started:{_master_namespace()}'


def _missing_latest_key() -> str:
    return f'{_MISSING_PREFIX}:latest:{_master_namespace()}'


def _missing_member(user_id: object, asset: str) -> str:
    return f'{user_id}|{asset}'


def _metric_key(name: str) -> str:
    return f'{_METRIC_PREFIX}{name}'


def _decode(raw: object) -> str:
    return raw.decode() if isinstance(raw, bytes) else str(raw)


async def cache_master_configs(
    redis: Redis,
    configs: dict[str, PositionConfig],
    *,
    master_equity: Decimal,
    now: float | None = None,
) -> None:
    """Persist timestamp-correlated master config and equity for a short bridge.

    The cache is populated only from one verified Hyperliquid account snapshot.
    Leverage and equity therefore share the same observation timestamp. Values
    expire at the existing snapshot stale boundary and are never guessed.
    """

    if master_equity <= 0:
        return
    observed_at = time.time() if now is None else now
    ttl = max(1, int(math.ceil(settings.HL_MASTER_SNAPSHOT_STALE_SECONDS)))
    for asset, config in configs.items():
        payload = json.dumps(
            {
                'leverage': int(config.leverage),
                'is_cross': bool(config.is_cross),
                'master_equity': str(master_equity),
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
        payload = json.loads(_decode(raw))
        observed_at = float(payload['observed_at'])
        age = max(current - observed_at, 0.0)
        if age > settings.HL_MASTER_SNAPSHOT_STALE_SECONDS:
            await redis.delete(_cache_key(asset))
            await redis.incr(_metric_key('master_leverage_shared_cache_stale_count'))
            return None
        leverage = max(int(payload['leverage']), 1)
        master_equity = Decimal(str(payload['master_equity']))
        if master_equity <= 0:
            raise ValueError('cached master equity must be positive')
        config = PositionConfig(leverage=leverage, is_cross=bool(payload['is_cross']))
        await redis.incr(_metric_key('master_leverage_shared_cache_hit_count'))
        await redis.set(_metric_key('master_leverage_shared_cache_age_seconds'), f'{age:.6f}')
        return CachedMasterLeverage(
            config=config,
            master_equity=master_equity,
            age_seconds=age,
        )
    except Exception:
        try:
            await redis.incr(_metric_key('master_leverage_shared_cache_error_count'))
        except Exception:
            pass
        return None


async def record_master_leverage_missing(
    redis: Redis,
    user_id: object,
    asset: str,
    *,
    intent_created_at: float,
) -> bool:
    """Track one continuous outage plus the newest blocked intent atomically.

    ``outage_started_at`` stays fixed at the first blocked job for the current
    outage, while ``latest_blocked_intent_at`` advances to the newest blocked
    job. Retries therefore cannot reset the SLO, but stale reconciliation
    evidence still cannot clear a newer blocked intent. Neither marker expires;
    explicit authoritative repair is required.
    """

    member = _missing_member(user_id, asset)
    created_raw = await redis.eval(
        _ATOMIC_MISSING_SCRIPT,
        2,
        _missing_started_key(),
        _missing_latest_key(),
        member,
        f'{float(intent_created_at):.6f}',
    )
    created = bool(int(created_raw or 0))
    if created:
        await redis.incr(_metric_key('master_leverage_unavailable_count'))
    return created


async def record_master_leverage_repaired(
    redis: Redis,
    user_id: object,
    asset: str,
    *,
    evidence_created_at: float | None = None,
    now: float | None = None,
) -> float | None:
    """Resolve only when authoritative evidence covers the newest intent.

    Recovery admissibility compares the evidence timestamp with the latest
    blocked intent. Recovery duration and SLO age remain anchored to the first
    blocked job of the continuous outage. Compare and removal are atomic, so a
    concurrent newer intent cannot be erased by stale reconciliation evidence.
    """

    if evidence_created_at is None:
        return None
    current = time.time() if now is None else now
    member = _missing_member(user_id, asset)
    raw_started = await redis.eval(
        _ATOMIC_REPAIR_SCRIPT,
        2,
        _missing_started_key(),
        _missing_latest_key(),
        member,
        f'{float(evidence_created_at):.6f}',
    )
    if raw_started in (None, False):
        return None
    started_at = float(_decode(raw_started))
    duration = max(current - started_at, 0.0)
    await redis.incr(_metric_key('master_leverage_recovery_count'))
    await redis.set(_metric_key('master_leverage_recovery_last_seconds'), f'{duration:.6f}')

    max_key = _metric_key('master_leverage_recovery_max_seconds')
    previous_raw = await redis.get(max_key)
    try:
        previous = float(_decode(previous_raw)) if previous_raw else 0.0
    except (TypeError, ValueError):
        previous = 0.0
    if duration > previous:
        await redis.set(max_key, f'{duration:.6f}')

    if duration > settings.MASTER_LEVERAGE_RECOVERY_SLO_SECONDS:
        await redis.incr(_metric_key('master_leverage_recovery_slo_breach_count'))
    return duration


async def master_leverage_metric_snapshot(
    redis: Redis,
    *,
    now: float | None = None,
) -> dict[str, float | int]:
    current = time.time() if now is None else now
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

    active = 0
    max_age = 0.0
    active_breaches = 0
    for raw_member, raw_score in await redis.zrange(_missing_started_key(), 0, -1, withscores=True):
        member = _decode(raw_member)
        if '|' not in member:
            await redis.zrem(_missing_started_key(), member)
            await redis.zrem(_missing_latest_key(), member)
            continue
        started_at = float(raw_score)
        age = max(current - started_at, 0.0)
        active += 1
        max_age = max(max_age, age)
        if age > settings.MASTER_LEVERAGE_RECOVERY_SLO_SECONDS:
            active_breaches += 1

    out['master_leverage_missing_active_count'] = active
    out['master_leverage_missing_max_age_seconds'] = max_age
    out['master_leverage_missing_slo_breach_active_count'] = active_breaches
    return out
