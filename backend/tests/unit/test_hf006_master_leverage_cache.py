from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.adapters.hyperliquid import PositionConfig
from app.core.config import settings
from app.services.master_leverage_cache import (
    cache_master_configs,
    cached_master_config,
    master_leverage_metric_snapshot,
    record_master_leverage_missing,
    record_master_leverage_repaired,
)
from app.services.queue import reconcile_job_repairs_missing_leverage
from app.workers import watcher as watcher_module
from app.workers.watcher import Watcher


class FakeRedis:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.zsets: dict[str, dict[str, float]] = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[str(key)] = str(value)
        return True

    async def get(self, key):
        return self.values.get(str(key))

    async def delete(self, key):
        self.values.pop(str(key), None)
        self.zsets.pop(str(key), None)
        return 1

    async def incr(self, key):
        name = str(key)
        value = int(float(self.values.get(name, '0'))) + 1
        self.values[name] = str(value)
        return value

    async def zadd(self, key, mapping, gt=False):
        values = self.zsets.setdefault(str(key), {})
        added = 0
        for member, raw_score in mapping.items():
            item = str(member)
            score = float(raw_score)
            if item not in values:
                values[item] = score
                added += 1
            elif not gt or score > values[item]:
                values[item] = score
        return added

    async def zscore(self, key, member):
        return self.zsets.get(str(key), {}).get(str(member))

    async def zrem(self, key, *members):
        values = self.zsets.setdefault(str(key), {})
        removed = 0
        for member in members:
            item = str(member)
            if item in values:
                del values[item]
                removed += 1
        return removed

    async def zrange(self, key, start, stop, withscores=False):
        ordered = sorted(self.zsets.get(str(key), {}).items(), key=lambda item: (item[1], item[0]))
        if stop == -1:
            selected = ordered[start:]
        else:
            selected = ordered[start:stop + 1]
        return selected if withscores else [member for member, _ in selected]

    async def eval(self, script, numkeys, key, member, evidence_created_at):
        assert numkeys == 1
        values = self.zsets.setdefault(str(key), {})
        item = str(member)
        current = values.get(item)
        if current is None or float(evidence_created_at) < current:
            return None
        del values[item]
        return str(current)


class DummySession:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.fixture
def configured_master(monkeypatch):
    monkeypatch.setattr(settings, 'HYPERLIQUID_MASTER_ADDRESS', '0xabc')
    monkeypatch.setattr(settings, 'HL_MASTER_SNAPSHOT_STALE_SECONDS', 15.0)
    monkeypatch.setattr(settings, 'MASTER_LEVERAGE_RECOVERY_SLO_SECONDS', 60.0)


def _watcher_with_cached_equity(redis: FakeRedis) -> Watcher:
    watcher = object.__new__(Watcher)
    watcher.redis = redis
    watcher.lease = SimpleNamespace(token=7)
    watcher._snapshot = None
    watcher._snapshot_at = 0.0
    watcher._equity = Decimal('1000')
    watcher._equity_at = asyncio.get_running_loop().time()
    watcher._background_tasks = set()
    return watcher


@pytest.mark.asyncio
async def test_shared_cache_returns_timestamp_correlated_leverage_and_equity(configured_master):
    redis = FakeRedis()
    await cache_master_configs(
        redis,
        {'BTC': PositionConfig(leverage=7, is_cross=False)},
        master_equity=Decimal('900'),
        now=100.0,
    )

    cached = await cached_master_config(redis, 'BTC', now=110.0)

    assert cached is not None
    assert cached.config == PositionConfig(leverage=7, is_cross=False)
    assert cached.master_equity == Decimal('900')
    assert cached.age_seconds == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_shared_cache_fails_closed_when_entry_is_stale(configured_master):
    redis = FakeRedis()
    await cache_master_configs(
        redis,
        {'ETH': PositionConfig(leverage=4, is_cross=True)},
        master_equity=Decimal('900'),
        now=100.0,
    )

    cached = await cached_master_config(redis, 'ETH', now=116.0)

    assert cached is None
    metrics = await master_leverage_metric_snapshot(redis, now=116.0)
    assert metrics['master_leverage_shared_cache_stale_count'] == 1


@pytest.mark.asyncio
async def test_shared_cache_fails_closed_for_malformed_or_unpaired_equity(configured_master):
    redis = FakeRedis()
    await cache_master_configs(
        redis,
        {'SOL': PositionConfig(leverage=3, is_cross=True)},
        master_equity=Decimal('900'),
        now=100.0,
    )
    cache_key = next(key for key in redis.values if key.startswith('hypercopy:master-leverage-cache:'))
    redis.values[cache_key] = json.dumps({'leverage': 3, 'is_cross': True, 'observed_at': 100.0})

    cached = await cached_master_config(redis, 'SOL', now=105.0)

    assert cached is None
    metrics = await master_leverage_metric_snapshot(redis, now=105.0)
    assert metrics['master_leverage_shared_cache_error_count'] == 1


@pytest.mark.asyncio
async def test_retry_of_same_blocked_job_does_not_reset_slo(configured_master):
    redis = FakeRedis()

    created = await record_master_leverage_missing(redis, 'user-1', 'BTC', intent_created_at=100.0)
    duplicate = await record_master_leverage_missing(redis, 'user-1', 'BTC', intent_created_at=100.0)
    metrics = await master_leverage_metric_snapshot(redis, now=161.0)

    assert created is True
    assert duplicate is False
    assert metrics['master_leverage_unavailable_count'] == 1
    assert metrics['master_leverage_missing_active_count'] == 1
    assert metrics['master_leverage_missing_max_age_seconds'] == pytest.approx(61.0)
    assert metrics['master_leverage_missing_slo_breach_active_count'] == 1
    assert metrics['master_leverage_recovery_count'] == 0


@pytest.mark.asyncio
async def test_newest_blocked_intent_controls_atomic_recovery_causality(configured_master):
    redis = FakeRedis()
    await record_master_leverage_missing(redis, 'user-1', 'BTC', intent_created_at=100.0)
    await record_master_leverage_missing(redis, 'user-1', 'BTC', intent_created_at=120.0)

    stale_repair = await record_master_leverage_repaired(
        redis,
        'user-1',
        'BTC',
        evidence_created_at=110.0,
        now=161.0,
    )
    pending = await master_leverage_metric_snapshot(redis, now=161.0)

    assert stale_repair is None
    assert pending['master_leverage_missing_active_count'] == 1
    assert pending['master_leverage_missing_max_age_seconds'] == pytest.approx(41.0)
    assert pending['master_leverage_recovery_count'] == 0

    fresh_repair = await record_master_leverage_repaired(
        redis,
        'user-1',
        'BTC',
        evidence_created_at=121.0,
        now=162.0,
    )
    repaired = await master_leverage_metric_snapshot(redis, now=162.0)

    assert fresh_repair == pytest.approx(42.0)
    assert repaired['master_leverage_missing_active_count'] == 0
    assert repaired['master_leverage_recovery_count'] == 1


@pytest.mark.asyncio
async def test_unresolved_marker_remains_visible_beyond_24_hours(configured_master):
    redis = FakeRedis()
    await record_master_leverage_missing(redis, 'user-1', 'BTC', intent_created_at=100.0)

    metrics = await master_leverage_metric_snapshot(redis, now=100.0 + 3 * 86_400)

    assert metrics['master_leverage_missing_active_count'] == 1
    assert metrics['master_leverage_missing_max_age_seconds'] == pytest.approx(3 * 86_400)
    assert metrics['master_leverage_missing_slo_breach_active_count'] == 1


@pytest.mark.asyncio
async def test_reconcile_repair_closes_active_marker_and_records_recovery(configured_master):
    redis = FakeRedis()
    await record_master_leverage_missing(redis, 'user-1', 'BTC', intent_created_at=100.0)

    duration = await record_master_leverage_repaired(
        redis,
        'user-1',
        'BTC',
        evidence_created_at=150.0,
        now=165.0,
    )
    metrics = await master_leverage_metric_snapshot(redis, now=165.0)

    assert duration == pytest.approx(65.0)
    assert metrics['master_leverage_missing_active_count'] == 0
    assert metrics['master_leverage_missing_slo_breach_active_count'] == 0
    assert metrics['master_leverage_recovery_count'] == 1
    assert metrics['master_leverage_recovery_last_seconds'] == pytest.approx(65.0)
    assert metrics['master_leverage_recovery_max_seconds'] == pytest.approx(65.0)
    assert metrics['master_leverage_recovery_slo_breach_count'] == 1
    assert metrics['master_leverage_recovery_slo_seconds'] == pytest.approx(60.0)


def test_only_authoritative_reconcile_intent_marks_missing_leverage_repaired():
    with_leverage = SimpleNamespace(
        origin='RECONCILE',
        context={'master_position': '1', 'master_leverage': 6},
    )
    superseding_close = SimpleNamespace(
        origin='RECONCILE',
        context={'master_position': '0', 'master_leverage': None},
    )
    still_unsafe = SimpleNamespace(
        origin='RECONCILE',
        context={'master_position': '1', 'master_leverage': None},
    )
    event_job = SimpleNamespace(
        origin='EVENT',
        context={'master_position': '1', 'master_leverage': 6},
    )

    assert reconcile_job_repairs_missing_leverage(with_leverage)
    assert reconcile_job_repairs_missing_leverage(superseding_close)
    assert not reconcile_job_repairs_missing_leverage(still_unsafe)
    assert not reconcile_job_repairs_missing_leverage(event_job)


@pytest.mark.asyncio
async def test_watcher_uses_only_correlated_shared_equity_with_cached_leverage(configured_master, monkeypatch):
    redis = FakeRedis()
    await cache_master_configs(
        redis,
        {'BTC': PositionConfig(leverage=6, is_cross=False)},
        master_equity=Decimal('900'),
        now=time.time(),
    )
    watcher = _watcher_with_cached_equity(redis)

    async def unavailable_snapshot(*, force_refresh=False):
        raise RuntimeError('temporary master state outage')

    captured: dict = {}

    async def fake_persist(db, **kwargs):
        captured.update(kwargs)
        return None, []

    monkeypatch.setattr(watcher, 'master_snapshot', unavailable_snapshot)
    monkeypatch.setattr(watcher_module, 'SessionLocal', lambda: DummySession())
    monkeypatch.setattr(watcher_module, 'persist_master_fill_and_jobs', fake_persist)

    await watcher.process_fill({'coin': 'BTC', 'sz': '0.1', 'px': '100', 'startPosition': '0'})

    assert captured['master_leverage'] == 6
    assert captured['master_is_cross'] is False
    assert captured['master_equity'] == Decimal('900')
    metrics = await master_leverage_metric_snapshot(redis)
    assert metrics['master_leverage_shared_cache_hit_count'] == 1


@pytest.mark.asyncio
async def test_watcher_keeps_fail_closed_when_live_and_shared_leverage_are_missing(configured_master, monkeypatch):
    redis = FakeRedis()
    watcher = _watcher_with_cached_equity(redis)

    async def unavailable_snapshot(*, force_refresh=False):
        raise RuntimeError('temporary master state outage')

    captured: dict = {}

    async def fake_persist(db, **kwargs):
        captured.update(kwargs)
        return None, []

    monkeypatch.setattr(watcher, 'master_snapshot', unavailable_snapshot)
    monkeypatch.setattr(watcher_module, 'SessionLocal', lambda: DummySession())
    monkeypatch.setattr(watcher_module, 'persist_master_fill_and_jobs', fake_persist)

    await watcher.process_fill({'coin': 'ETH', 'sz': '0.1', 'px': '100', 'startPosition': '0'})

    assert captured['master_leverage'] is None
    assert captured['master_is_cross'] is None
    assert captured['master_equity'] == Decimal('1000')
    assert all('master-leverage-missing' not in key for key in redis.zsets)
