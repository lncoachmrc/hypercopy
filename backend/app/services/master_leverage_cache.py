from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy import text

from app.adapters.hyperliquid import PositionConfig
from app.core.config import settings
from app.db.session import SessionLocal
from app.models.entities import SystemFlag

_CACHE_PREFIX = 'hypercopy:master-leverage-cache'
_METRIC_PREFIX = 'hypercopy:metrics:'
_MISSING_PREFIX = 'hypercopy:master-leverage-missing'
_MAX_CAUSAL_ORDER = 9_007_199_254_740_991
_ATOMIC_MISSING_SCRIPT = """
-- HF006_REGISTER_MISSING
local member = ARGV[1]
local intent_order = tonumber(ARGV[2])
if not intent_order then
  return 0
end
local repaired = redis.call('ZSCORE', KEYS[3], member)
if repaired and tonumber(repaired) >= intent_order then
  return 0
end
local started = redis.call('ZSCORE', KEYS[1], member)
local created = 0
if not started then
  local current
  if ARGV[3] ~= '' then
    current = tonumber(ARGV[3])
  else
    local redis_time = redis.call('TIME')
    current = tonumber(redis_time[1]) + tonumber(redis_time[2]) / 1000000
  end
  redis.call('ZADD', KEYS[1], current, member)
  redis.call('INCR', KEYS[4])
  created = 1
end
local latest = redis.call('ZSCORE', KEYS[2], member)
if not latest or intent_order > tonumber(latest) then
  redis.call('ZADD', KEYS[2], intent_order, member)
end
return created
"""
_ATOMIC_REPAIR_SCRIPT = """
-- HF006_RECORD_REPAIR
local member = ARGV[1]
local evidence_order = tonumber(ARGV[2])
local slo_seconds = tonumber(ARGV[3])
if not evidence_order or not slo_seconds then
  return false
end
local previous_repair = redis.call('ZSCORE', KEYS[3], member)
if not previous_repair or evidence_order > tonumber(previous_repair) then
  redis.call('ZADD', KEYS[3], evidence_order, member)
end
local started = redis.call('ZSCORE', KEYS[1], member)
local latest = redis.call('ZSCORE', KEYS[2], member)
if not started or not latest then
  return false
end
if evidence_order < tonumber(latest) then
  return false
end
local current
if ARGV[4] ~= '' then
  current = tonumber(ARGV[4])
else
  local redis_time = redis.call('TIME')
  current = tonumber(redis_time[1]) + tonumber(redis_time[2]) / 1000000
end
local duration = current - tonumber(started)
if duration < 0 then
  duration = 0
end
redis.call('ZREM', KEYS[1], member)
redis.call('ZREM', KEYS[2], member)
redis.call('INCR', KEYS[4])
redis.call('SET', KEYS[5], string.format('%.6f', duration))
local previous_max = tonumber(redis.call('GET', KEYS[6]) or '0')
if duration > previous_max then
  redis.call('SET', KEYS[6], string.format('%.6f', duration))
end
if duration > slo_seconds then
  redis.call('INCR', KEYS[7])
end
return string.format('%.6f', duration)
"""
_ATOMIC_PUBLISH_REPAIR_SCRIPT = """
-- HF006_PUBLISH_REPAIR
local member = ARGV[1]
local evidence_order = tonumber(ARGV[2])
local slo_seconds = tonumber(ARGV[3])
local job_id = ARGV[4]
local maxlen = tonumber(ARGV[5])
if not evidence_order or not slo_seconds or not maxlen or not job_id or job_id == '' then
  return redis.error_reply('invalid HF006 publish-repair arguments')
end
local message_id = redis.call('XADD', KEYS[1], 'MAXLEN', '~', maxlen, '*', 'job_id', job_id)
local previous_repair = redis.call('ZSCORE', KEYS[4], member)
if not previous_repair or evidence_order > tonumber(previous_repair) then
  redis.call('ZADD', KEYS[4], evidence_order, member)
end
local started = redis.call('ZSCORE', KEYS[2], member)
local latest = redis.call('ZSCORE', KEYS[3], member)
if not started or not latest or evidence_order < tonumber(latest) then
  return message_id
end
local current
if ARGV[6] ~= '' then
  current = tonumber(ARGV[6])
else
  local redis_time = redis.call('TIME')
  current = tonumber(redis_time[1]) + tonumber(redis_time[2]) / 1000000
end
local duration = current - tonumber(started)
if duration < 0 then
  duration = 0
end
redis.call('ZREM', KEYS[2], member)
redis.call('ZREM', KEYS[3], member)
redis.call('INCR', KEYS[5])
redis.call('SET', KEYS[6], string.format('%.6f', duration))
local previous_max = tonumber(redis.call('GET', KEYS[7]) or '0')
if duration > previous_max then
  redis.call('SET', KEYS[7], string.format('%.6f', duration))
end
if duration > slo_seconds then
  redis.call('INCR', KEYS[8])
end
return message_id
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


def _repair_latest_key() -> str:
    return f'{_MISSING_PREFIX}:repair:{_master_namespace()}'


def _causal_order_key() -> str:
    return f'{_MISSING_PREFIX}:order:{_master_namespace()}'


def _causal_order_slug() -> str:
    return f'hf006_causal_order:{_master_namespace()}'


def _missing_member(user_id: object, asset: str) -> str:
    return f'{user_id}|{asset}'


def _metric_key(name: str) -> str:
    return f'{_METRIC_PREFIX}{name}'


def _decode(raw: object) -> str:
    return raw.decode() if isinstance(raw, bytes) else str(raw)


async def _redis_now(redis: Redis) -> float:
    raw = await redis.time()
    if isinstance(raw, (tuple, list)) and len(raw) >= 2:
        return float(raw[0]) + float(raw[1]) / 1_000_000
    raise RuntimeError('Redis TIME returned an invalid payload')


async def next_master_leverage_causal_order(redis: Redis | None = None) -> int:
    """Allocate a durable master-namespace causal token from PostgreSQL.

    Redis cannot be the causal source for the Redis-outage fallback itself. The
    existing ``system_flags`` table therefore owns one monotonically increasing
    counter per master network/address namespace. A PostgreSQL advisory
    transaction lock serializes both the first insert and every later increment.

    The optional Redis write is a bounded best-effort compatibility/diagnostic
    mirror only. Repair admissibility never depends on that mirror.
    """

    slug = _causal_order_slug()
    async with SessionLocal() as db:
        await db.execute(
            text('SELECT pg_advisory_xact_lock(hashtextextended(:slug, 0))'),
            {'slug': slug},
        )
        row = await db.get(SystemFlag, slug, with_for_update=True)
        if row is None:
            value = 1
            row = SystemFlag(
                slug=slug,
                enabled=True,
                value={'order': value},
                reason='HF-006 durable master causal ordering',
            )
            db.add(row)
        else:
            raw = (row.value or {}).get('order', 0)
            try:
                current = int(raw)
            except Exception as exc:
                raise RuntimeError('HF-006 PostgreSQL causal order is malformed') from exc
            if current < 0 or current >= _MAX_CAUSAL_ORDER:
                raise RuntimeError('HF-006 PostgreSQL causal order is out of range')
            value = current + 1
            row.enabled = True
            row.value = {**(row.value or {}), 'order': value}
        await db.commit()

    if redis is not None:
        try:
            await asyncio.wait_for(
                redis.set(_causal_order_key(), str(value)),
                timeout=0.05,
            )
        except Exception:
            pass
    return value


async def cache_master_configs(
    redis: Redis,
    configs: dict[str, PositionConfig],
    *,
    master_equity: Decimal,
    now: float | None = None,
) -> None:
    """Persist timestamp-correlated master config and equity for a short bridge.

    The cache is populated only from one verified Hyperliquid account snapshot.
    Leverage and equity therefore share the same observation timestamp. Redis
    TIME is the default clock so readers in other processes cannot disagree due
    to host clock skew. Values expire at the existing snapshot stale boundary.
    """

    if master_equity <= 0:
        return
    observed_at = await _redis_now(redis) if now is None else now
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
    current = await _redis_now(redis) if now is None else now
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
    intent_order: int,
    now: float | None = None,
) -> bool:
    """Track one continuous outage plus the newest blocked intent atomically.

    ``outage_started_at`` uses Redis TIME and stays fixed at the first blocked
    job for the continuous outage. ``latest_blocked_intent_order`` is a stable
    causal token assigned when the intent is created. A repair watermark that
    already covers the intent suppresses late marker registration, closing the
    repair-before-registration race without resetting the SLO on retries.
    """

    order = int(intent_order)
    if order <= 0:
        return False
    member = _missing_member(user_id, asset)
    created_raw = await redis.eval(
        _ATOMIC_MISSING_SCRIPT,
        4,
        _missing_started_key(),
        _missing_latest_key(),
        _repair_latest_key(),
        _metric_key('master_leverage_unavailable_count'),
        member,
        str(order),
        '' if now is None else f'{float(now):.6f}',
    )
    return bool(int(created_raw or 0))


async def record_master_leverage_repaired(
    redis: Redis,
    user_id: object,
    asset: str,
    *,
    evidence_order: int | None = None,
    now: float | None = None,
) -> float | None:
    """Record authoritative repair with atomic marker removal and accounting.

    The repair watermark is written even when marker registration has not
    happened yet. If the marker exists, evidence must cover the newest blocked
    intent order. Removal, recovery count/duration/max and historical SLO breach
    accounting execute in the same Lua script, so a lost client reply cannot
    leave the active marker and recovery metrics in contradictory states.
    """

    if evidence_order is None:
        return None
    order = int(evidence_order)
    if order <= 0:
        return None
    member = _missing_member(user_id, asset)
    raw_duration = await redis.eval(
        _ATOMIC_REPAIR_SCRIPT,
        7,
        _missing_started_key(),
        _missing_latest_key(),
        _repair_latest_key(),
        _metric_key('master_leverage_recovery_count'),
        _metric_key('master_leverage_recovery_last_seconds'),
        _metric_key('master_leverage_recovery_max_seconds'),
        _metric_key('master_leverage_recovery_slo_breach_count'),
        member,
        str(order),
        f'{float(settings.MASTER_LEVERAGE_RECOVERY_SLO_SECONDS):.6f}',
        '' if now is None else f'{float(now):.6f}',
    )
    if raw_duration in (None, False):
        return None
    return float(_decode(raw_duration))


async def publish_master_leverage_repair(
    redis: Redis,
    *,
    stream_name: str,
    job_id: object,
    user_id: object,
    asset: str,
    evidence_order: int,
    maxlen: int = 100_000,
    now: float | None = None,
) -> str:
    """Atomically publish a repair job and record its recovery bookkeeping.

    A successful script execution guarantees that the corrective stream entry,
    repair watermark, eligible marker removal and recovery metrics moved
    together. If the client loses the reply after execution, a later republish
    is safe: the durable job state prevents duplicate execution and absent
    active markers prevent duplicate recovery accounting.
    """

    order = int(evidence_order)
    if order <= 0 or maxlen <= 0:
        raise ValueError('HF006 repair publish requires positive order and maxlen')
    raw_message_id = await redis.eval(
        _ATOMIC_PUBLISH_REPAIR_SCRIPT,
        8,
        stream_name,
        _missing_started_key(),
        _missing_latest_key(),
        _repair_latest_key(),
        _metric_key('master_leverage_recovery_count'),
        _metric_key('master_leverage_recovery_last_seconds'),
        _metric_key('master_leverage_recovery_max_seconds'),
        _metric_key('master_leverage_recovery_slo_breach_count'),
        _missing_member(user_id, asset),
        str(order),
        f'{float(settings.MASTER_LEVERAGE_RECOVERY_SLO_SECONDS):.6f}',
        str(job_id),
        str(int(maxlen)),
        '' if now is None else f'{float(now):.6f}',
    )
    if not raw_message_id:
        raise RuntimeError('HF006 atomic repair publish returned no stream id')
    return _decode(raw_message_id)


async def master_leverage_metric_snapshot(
    redis: Redis,
    *,
    now: float | None = None,
) -> dict[str, float | int]:
    current = await _redis_now(redis) if now is None else now
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