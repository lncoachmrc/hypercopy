"""Worker P0: durable observed cursor, Redis dedup and singleton advisory lock."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.workers import ai_intelligence_worker as module


@pytest.mark.asyncio
async def test_expired_lookback_cursor_does_not_trigger_old_event_again():
    worker = module.AIIntelligenceWorker.__new__(module.AIIntelligenceWorker)
    old_ts = datetime(2026, 10, 3, tzinfo=UTC)

    async def state():
        return {
            'status': 'insufficient_data',
            'source_event_ts': None,
            'observed_source_event_ts': old_ts.isoformat(),
        }

    worker._state = state
    await worker._sync_source_from_state()
    assert worker._source_ts == old_ts
    assert worker._is_new_event(SimpleNamespace(event_ts=old_ts)) is False
    assert worker._is_new_event(SimpleNamespace(event_ts=old_ts + timedelta(seconds=1))) is True


@pytest.mark.asyncio
async def test_duplicate_redis_signal_does_not_mark_pending():
    worker = module.AIIntelligenceWorker.__new__(module.AIIntelligenceWorker)
    ts = datetime(2026, 10, 3, tzinfo=UTC)

    class Redis:
        async def brpop(self, *_args, **_kwargs):
            return ('events', json.dumps({'event_ts': ts.isoformat()}))

    worker.redis = Redis()
    worker.queue = 'events'
    worker.poll = 1
    worker._source_ts = ts
    worker._pending = False
    worker._last_signal_mono = None

    assert await worker._consume_signal() is False
    assert worker._pending is False


@pytest.mark.asyncio
async def test_db_fallback_detects_new_event_after_stale_cursor():
    worker = module.AIIntelligenceWorker.__new__(module.AIIntelligenceWorker)
    worker._source_ts = datetime(2026, 10, 3, tzinfo=UTC)
    worker._last_db_poll = 0.0
    worker.db_poll = 1
    worker._pending = False
    worker._last_signal_mono = None
    worker.debounce = 0

    async def latest():
        return SimpleNamespace(event_ts=datetime.now(UTC))

    worker._latest_master_event = latest
    await worker._db_fallback_check()
    assert worker._pending is True
    assert worker._last_signal_mono is not None


class _Scalar:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class _Connection:
    def __init__(self, engine):
        self.engine = engine

    async def execute(self, statement):
        sql = str(statement)
        if 'pg_try_advisory_lock' in sql:
            if self.engine.locked:
                return _Scalar(False)
            self.engine.locked = True
            return _Scalar(True)
        if 'pg_advisory_unlock' in sql:
            assert self.engine.locked
            self.engine.locked = False
            return _Scalar(True)
        raise AssertionError(sql)

    async def commit(self):
        pass


class _Engine:
    def __init__(self):
        self.locked = False

    @asynccontextmanager
    async def connect(self):
        yield _Connection(self)


@pytest.mark.asyncio
async def test_two_workers_cannot_call_llm_under_the_same_advisory_lock(monkeypatch):
    engine = _Engine()
    started = asyncio.Event()
    finish = asyncio.Event()
    calls = []

    @asynccontextmanager
    async def session():
        yield object()

    async def refresh(_db, *, force):
        assert force is True
        calls.append(1)
        started.set()
        await finish.wait()
        return {
            'status': 'ok',
            'observed_source_event_ts': datetime(2026, 10, 3, tzinfo=UTC).isoformat(),
            'provider': 'openai',
            'model': 'test-model',
            'fallback_index': 0,
        }

    monkeypatch.setattr(module, 'engine', engine)
    monkeypatch.setattr(module, 'SessionLocal', session)
    monkeypatch.setattr(module, 'refresh_ai_intelligence', refresh)

    one = module.AIIntelligenceWorker.__new__(module.AIIntelligenceWorker)
    two = module.AIIntelligenceWorker.__new__(module.AIIntelligenceWorker)
    one._source_ts = None
    two._source_ts = None

    first_task = asyncio.create_task(one._run_analysis('first'))
    await asyncio.wait_for(started.wait(), timeout=2)
    assert await two._run_analysis('second') is False
    assert len(calls) == 1

    finish.set()
    assert await first_task is True
    assert engine.locked is False
    assert one._source_ts == datetime(2026, 10, 3, tzinfo=UTC)
