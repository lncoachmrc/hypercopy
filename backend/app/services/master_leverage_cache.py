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


def _missing_index_key() -> str:
    return f'{_MISSING_PREFIX}:index:{_master_namespace()}'


def _missing_member(user_id: object, asset: str) -> str:
    return f'{user_id}|{asset}'


def _missing_key(user_id: object, asset: str) -> str:
    return f'{_MISSING_PREFIX}:{_master_namespace()}:{user_id}:{asset}'


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
    now: float | None = None,
) -> bool:
    """Track one durable follower intent blocked by missing master leverage.

    Index membership is repaired on every call, including when the marker was
    already created by an earlier attempt whose SADD failed. This keeps active
    SLO telemetry recoverable after transient Redis partial failures.
    """

    current = time.time() if now is None else now
    created = bool(await redis.set(
        _missing_key(user_id, asset),
        f'{current:.6f}',
        nx=True,
        ex=86_400,
    ))
    await redis.sadd(_missing_index_key(), _missing_member(user_id, asset))
    await redis.expire(_missing_index_key(), 86_400)
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
    """Resolve a blocked intent only after a newer reconciliation supersedes it.

    A queued RECONCILE created before the missing EVENT is not valid recovery
    evidence even if it is republished later. Its creation timestamp must be at
    or after the missing marker timestamp before telemetry can be closed.
    """

    current = time.time() if now is None else now
    key = _missing_key(user_id, asset)
    member = _missing_member(user_id, asset)
    raw = await redis.get(key)
    if not raw:
        await redis.srem(_missing_index_key(), member)
        return None
    try:
        started_at = float(_decode(raw))
    except (TypeError, ValueError):
        await redis.delete(key)
        await redis.srem(_missing_index_key(), member)
        return None

    if evidence_created_at is None or evidence_created_at < started_at:
        return None

    duration = max(current - started_at, 0.0)
    await redis.delete(key)
    await redis.srem(_missing_index_key(), member)
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
    members = await redis.smembers(_missing_index_key())
    for raw_member in members:
        member = _decode(raw_member)
        try:
            user_id, asset = member.rsplit('|', 1)
        except ValueError:
            await redis.srem(_missing_index_key(), member)
            continue
        raw_started = await redis.get(_missing_key(user_id, asset))
        if not raw_started:
            await redis.srem(_missing_index_key(), member)
            continue
        try:
            started_at = float(_decode(raw_started))
        except (TypeError, ValueError):
            await redis.delete(_missing_key(user_id, asset))
            await redis.srem(_missing_index_key(), member)
            continue
        age = max(current - started_at, 0.0)
        active += 1
        max_age = max(max_age, age)
        if age > settings.MASTER_LEVERAGE_RECOVERY_SLO_SECONDS:
            active_breaches += 1

    out['master_leverage_missing_active_count'] = active
    out['master_leverage_missing_max_age_seconds'] = max_age
    out['master_leverage_missing_slo_breach_active_count'] = active_breaches
    return out
