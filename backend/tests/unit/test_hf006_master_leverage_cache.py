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
    record_master_leverage_available,
    record_master_leverage_missing,
)
from app.workers import watcher as watcher_module
from app.workers.watcher import Watcher


class FakeRedis:
    def __init__(self):
        self.values: dict[str, str] = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[str(key)] = str(value)
        return True

    async def get(self, key):
        return self.values.get(str(key))

    async def delete(self, key):
        self.values.pop(str(key), None)
        return 1

    async def incr(self, key):
        name = str(key)
        value = int(float(self.values.get(name, '0'))) + 1
        self.values[name] = str(value)
        return value


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
async def test_shared_cache_returns_only_fresh_verified_master_config(configured_master):
    redis = FakeRedis()
    await cache_master_configs(
        redis,
        {'BTC': PositionConfig(leverage=7, is_cross=False)},
        now=100.0,
    )

    cached = await cached_master_config(redis, 'BTC', now=110.0)

    assert cached is not None
    assert cached.config == PositionConfig(leverage=7, is_cross=False)
    assert cached.age_seconds == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_shared_cache_fails_closed_when_entry_is_stale(configured_master):
    redis = FakeRedis()
    await cache_master_configs(
        redis,
        {'ETH': PositionConfig(leverage=4, is_cross=True)},
        now=100.0,
    )

    cached = await cached_master_config(redis, 'ETH', now=116.0)

    assert cached is None
    metrics = await master_leverage_metric_snapshot(redis)
    assert metrics['master_leverage_shared_cache_stale_count'] == 1


@pytest.mark.asyncio
async def test_shared_cache_fails_closed_for_malformed_payload(configured_master):
    redis = FakeRedis()
    await cache_master_configs(
        redis,
        {'SOL': PositionConfig(leverage=3, is_cross=True)},
        now=100.0,
    )
    cache_key = next(key for key in redis.values if key.startswith('hypercopy:master-leverage-cache:'))
    redis.values[cache_key] = json.dumps({'leverage': 'bad', 'is_cross': True, 'observed_at': 100.0})

    cached = await cached_master_config(redis, 'SOL', now=105.0)

    assert cached is None
    metrics = await master_leverage_metric_snapshot(redis)
    assert metrics['master_leverage_shared_cache_error_count'] == 1


@pytest.mark.asyncio
async def test_missing_leverage_recovery_duration_and_slo_are_measured(configured_master):
    redis = FakeRedis()

    await record_master_leverage_missing(redis, 'BTC', now=100.0)
    duration = await record_master_leverage_available(redis, 'BTC', now=145.0)

    assert duration == pytest.approx(45.0)
    metrics = await master_leverage_metric_snapshot(redis)
    assert metrics['master_leverage_unavailable_count'] == 1
    assert metrics['master_leverage_recovery_count'] == 1
    assert metrics['master_leverage_recovery_last_seconds'] == pytest.approx(45.0)
    assert metrics['master_leverage_recovery_max_seconds'] == pytest.approx(45.0)
    assert metrics['master_leverage_recovery_slo_breach_count'] == 0
    assert metrics['master_leverage_recovery_slo_seconds'] == pytest.approx(60.0)


@pytest.mark.asyncio
async def test_recovery_beyond_slo_is_counted(configured_master):
    redis = FakeRedis()

    await record_master_leverage_missing(redis, 'BTC', now=100.0)
    duration = await record_master_leverage_available(redis, 'BTC', now=161.0)

    assert duration == pytest.approx(61.0)
    metrics = await master_leverage_metric_snapshot(redis)
    assert metrics['master_leverage_recovery_slo_breach_count'] == 1


@pytest.mark.asyncio
async def test_watcher_uses_fresh_shared_cache_after_live_snapshot_failure(configured_master, monkeypatch):
    redis = FakeRedis()
    await cache_master_configs(
        redis,
        {'BTC': PositionConfig(leverage=6, is_cross=False)},
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
    metrics = await master_leverage_metric_snapshot(redis)
    assert metrics['master_leverage_unavailable_count'] == 1
