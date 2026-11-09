"""P0: deduplicate Capital Intelligence and fail closed without master evidence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
from decimal import Decimal

import pytest

from app.models.entities import MasterEvent
from app.services import ai_intelligence as intelligence
from app.services.ai_mode import derive_ai_execution_policy


class _Result:
    def __init__(self, values):
        self.values = values

    def scalar_one_or_none(self):
        return self.values[0] if self.values else None

    def scalars(self):
        return self

    def all(self):
        return self.values


class _Session:
    """Minimal SQLAlchemy session double: exercises the real refresh branches."""

    def __init__(self, events=()):
        self.events = list(events)
        self.row = None
        self.commits = 0

    async def get(self, _model, _slug):
        return self.row

    async def execute(self, stmt):
        ordered = sorted(
            self.events,
            key=lambda item: (item.event_ts, item.exchange_event_id),
            reverse=True,
        )
        if stmt.whereclause is not None:
            days = max(min(int(os.getenv('LLM_LOOKBACK_DAYS', '30')), 180), 1)
            cutoff = datetime.now(UTC) - timedelta(days=days)
            return _Result([e for e in ordered if e.event_ts >= cutoff][:5000])
        return _Result(ordered[:1])

    def add(self, row):
        self.row = row

    async def commit(self):
        self.commits += 1
        if self.row is not None:
            self.row.updated_at = datetime.now(UTC)


def _event(suffix, *, age_minutes=1):
    return MasterEvent(
        exchange_event_id=f'fill-{suffix}',
        asset='BTC',
        side='B',
        size=Decimal('1'),
        price=Decimal('100'),
        start_position=Decimal('0'),
        position_after=Decimal('1'),
        master_equity=Decimal('1000'),
        event_ts=datetime.now(UTC) - timedelta(minutes=age_minutes),
        raw={},
        fencing_token=1,
    )


def _enable(monkeypatch):
    monkeypatch.setenv('LLM_ENABLED', 'true')
    monkeypatch.setenv('LLM_MODE', 'shadow')
    monkeypatch.setenv('LLM_LOOKBACK_DAYS', '30')
    monkeypatch.setenv('LLM_PREFERRED_MODEL', 'openai:test-model')


def _fake_provider():
    calls = []

    async def invoke(_system, prompt):
        calls.append(prompt)
        return {
            'summary': 'Profile observed',
            'observed_patterns': [],
            'capital_policy': {'buffer_pct': 0.1},
            'confidence': 0.75,
        }, {
            'provider': 'openai',
            'model': 'test-model',
            'fallback_index': 0,
            'failures': [],
        }

    return calls, invoke


@pytest.mark.asyncio
async def test_cold_start_empty_db_skips_llm_and_fails_closed(monkeypatch):
    _enable(monkeypatch)
    calls, provider = _fake_provider()
    monkeypatch.setattr(intelligence, 'call_llm_with_failover', provider)
    db = _Session()

    state = await intelligence.refresh_ai_intelligence(db, force=True)

    assert state['status'] == 'insufficient_data'
    assert state['data_quality'] == 'no_master_history'
    assert state['strategy_profile']['lookback_events'] == 0
    assert state['analysis'] is None
    assert state['source_event_ts'] is None
    assert state['observed_source_event_ts'] is None
    assert calls == []
    assert derive_ai_execution_policy(state, requested=True).effective is False


@pytest.mark.asyncio
async def test_expired_source_cursor_persists_and_repeat_is_noop(monkeypatch):
    _enable(monkeypatch)
    calls, provider = _fake_provider()
    monkeypatch.setattr(intelligence, 'call_llm_with_failover', provider)
    old = _event('old', age_minutes=40 * 24 * 60)
    db = _Session([old])

    state = await intelligence.refresh_ai_intelligence(db, force=True)
    assert state['status'] == 'insufficient_data'
    assert state['data_quality'] == 'stale_source'
    assert state['source_event_ts'] is None
    assert state['observed_source_event_ts'] == old.event_ts.isoformat()
    assert state['observed_source_event_id'] == old.exchange_event_id
    assert state['analysis'] is None
    assert calls == []
    assert db.commits == 1

    again = await intelligence.refresh_ai_intelligence(db, force=True)
    assert again == state
    assert db.commits == 1
    assert calls == []


@pytest.mark.asyncio
async def test_new_fill_triggers_once_and_mode_change_recalculates(monkeypatch):
    _enable(monkeypatch)
    calls, provider = _fake_provider()
    monkeypatch.setattr(intelligence, 'call_llm_with_failover', provider)
    db = _Session([_event('one')])

    first = await intelligence.refresh_ai_intelligence(db, force=True)
    assert first['status'] == 'ok'
    assert first['strategy_profile']['lookback_events'] == 1
    assert len(calls) == 1

    assert await intelligence.refresh_ai_intelligence(db, force=True) == first
    assert db.commits == 1
    assert len(calls) == 1

    # Mode changes are part of the effective prompt, not cache hits.
    monkeypatch.setenv('LLM_MODE', 'on')
    changed = await intelligence.refresh_ai_intelligence(db, force=True)
    assert changed['mode'] == 'on'
    assert len(calls) == 2

    db.events.append(_event('two', age_minutes=0))
    new = await intelligence.refresh_ai_intelligence(db, force=True)
    assert new['strategy_profile']['lookback_events'] == 2
    assert new['observed_source_event_id'] == 'fill-two'
    assert len(calls) == 3
    assert await intelligence.refresh_ai_intelligence(db, force=True) == new
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_old_valid_analysis_is_audit_only_after_lookback_expires(monkeypatch):
    _enable(monkeypatch)
    calls, provider = _fake_provider()
    monkeypatch.setattr(intelligence, 'call_llm_with_failover', provider)
    db = _Session([_event('old', age_minutes=20 * 24 * 60)])

    healthy = await intelligence.refresh_ai_intelligence(db, force=True)
    assert healthy['status'] == 'ok'

    monkeypatch.setenv('LLM_LOOKBACK_DAYS', '1')
    expired = await intelligence.refresh_ai_intelligence(db, force=True)
    assert expired['status'] == 'insufficient_data'
    assert expired['analysis'] is None
    assert expired['last_valid_analysis']['analysis'] == healthy['analysis']
    assert expired['last_valid_analysis']['source_event_id'] == healthy['source_event_id']
    assert derive_ai_execution_policy(expired, requested=True).effective is False
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_provider_error_keeps_retry_open_and_never_enables_execution(monkeypatch):
    _enable(monkeypatch)
    calls = []

    async def flaky(*_args):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError('provider timed out')
        return {
            'summary': 'Valid',
            'capital_policy': {'buffer_pct': 0.1},
            'confidence': 0.8,
        }, {
            'provider': 'openai', 'model': 'test-model',
            'fallback_index': 0, 'failures': [],
        }

    monkeypatch.setattr(intelligence, 'call_llm_with_failover', flaky)
    db = _Session([_event('one')])

    failed = await intelligence.refresh_ai_intelligence(db, force=True)
    assert failed['status'] == 'degraded'
    assert failed['data_quality'] == 'provider_error'
    assert failed['observed_source_event_id'] == 'fill-one'
    assert derive_ai_execution_policy(failed, requested=True).effective is False

    retried = await intelligence.refresh_ai_intelligence(db, force=True)
    assert retried['status'] == 'ok'
    assert retried['strategy_profile']['lookback_events'] == 1
    assert len(calls) == 2

    await intelligence.refresh_ai_intelligence(db, force=True)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_disabled_mode_never_calls_llm_and_advances_observed_cursor(monkeypatch):
    monkeypatch.setenv('LLM_ENABLED', 'false')
    monkeypatch.setenv('LLM_MODE', 'on')
    calls, provider = _fake_provider()
    monkeypatch.setattr(intelligence, 'call_llm_with_failover', provider)
    db = _Session([_event('one')])

    first = await intelligence.refresh_ai_intelligence(db, force=True)
    assert first['status'] == 'disabled'
    assert first['analysis'] is None
    assert first['observed_source_event_id'] == 'fill-one'

    await intelligence.refresh_ai_intelligence(db, force=True)
    assert db.commits == 1

    db.events.append(_event('two', age_minutes=0))
    second = await intelligence.refresh_ai_intelligence(db, force=True)
    assert second['observed_source_event_id'] == 'fill-two'
    assert db.commits == 2
    assert calls == []
