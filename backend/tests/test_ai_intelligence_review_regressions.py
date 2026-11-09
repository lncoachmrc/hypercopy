"""PR 297 review contracts: real policy/worker behavior, no exchange or LLM I/O."""
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import ai_intelligence as intelligence
from app.services.ai_mode import derive_ai_execution_policy
from app.services.ai_profit_exit import ProfitExitFeatureMode
from app.workers import ai_intelligence_worker as module


@pytest.mark.parametrize('new_id', ['a-before-observed', 'z-after-observed'])
@pytest.mark.asyncio
async def test_db_fallback_finds_same_timestamp_fill_in_both_id_orders(new_id):
    stamp = datetime(2026, 10, 3, tzinfo=UTC)
    worker = module.AIIntelligenceWorker.__new__(module.AIIntelligenceWorker)
    worker._source_ts = stamp
    worker._source_boundary_count = 1
    worker._last_db_poll = float('-inf')
    worker.db_poll = 60
    worker.debounce = 0
    worker._pending = False
    worker._last_signal_mono = None
    worker._latest_master_event = AsyncMock(return_value=SimpleNamespace(
        event_ts=stamp, exchange_event_id=new_id, boundary_count=2,
    ))

    await worker._db_fallback_check()

    assert worker._pending is True
    assert worker._last_signal_mono is not None


@pytest.mark.asyncio
async def test_restart_restores_frontier_and_detects_equal_timestamp_arrival():
    stamp = datetime(2026, 10, 3, tzinfo=UTC)
    worker = module.AIIntelligenceWorker.__new__(module.AIIntelligenceWorker)
    worker._state = AsyncMock(return_value={
        'status': 'insufficient_data',
        'source_event_ts': None,
        'observed_source_event_ts': stamp.isoformat(),
        'observed_source_boundary_count': 1,
    })
    await worker._sync_source_from_state()
    assert getattr(worker, '_source_boundary_count', None) == 1
    assert worker._is_new_event(SimpleNamespace(event_ts=stamp, boundary_count=1)) is False
    assert worker._is_new_event(SimpleNamespace(event_ts=stamp, boundary_count=2)) is True


def test_legacy_cursor_without_count_is_rechecked_once():
    stamp = datetime(2026, 10, 3, tzinfo=UTC)
    worker = module.AIIntelligenceWorker.__new__(module.AIIntelligenceWorker)
    worker._source_ts = stamp
    worker._source_boundary_count = None
    assert worker._is_new_event(SimpleNamespace(event_ts=stamp, boundary_count=1)) is True
    worker._source_boundary_count = 1
    assert worker._is_new_event(SimpleNamespace(event_ts=stamp, boundary_count=1)) is False
    assert worker._is_new_event(SimpleNamespace(
        event_ts=stamp - timedelta(seconds=1), boundary_count=99,
    )) is False


@pytest.mark.parametrize('setting,new_value', [
    ('CAPITAL_INTELLIGENCE_SYSTEM_PROMPT', 'Changed system instructions'),
    ('MASTER_STRATEGY_FEATURE_VERSION', 999),
])
def test_model_input_fingerprint_invalidates_prompt_and_feature_changes(monkeypatch, setting, new_value):
    monkeypatch.setattr(intelligence, 'provider_chain', lambda: [('openai', 'test-model')])
    event = SimpleNamespace(
        exchange_event_id='test-fill', event_ts=datetime(2026, 10, 3, tzinfo=UTC),
        asset='BTC', size=Decimal('1'), price=Decimal('100'),
        start_position=Decimal('0'), position_after=Decimal('1'),
        master_equity=Decimal('1000'),
    )
    before = intelligence._analysis_input_fingerprint([event], mode='shadow', days=30)
    monkeypatch.setattr(intelligence, setting, new_value, raising=False)
    after = intelligence._analysis_input_fingerprint([event], mode='shadow', days=30)
    assert after != before
    assert after == intelligence._analysis_input_fingerprint([event], mode='shadow', days=30)


def _archived_state(status):
    return {
        'status': status,
        'analysis': None,
        'last_valid_analysis': {
            'analysis': {'capital_policy': {'buffer_pct': 0.30}},
            'strategy_profile': {'lookback_events': 5},
            'source_event_ts': '2026-10-03T12:00:00+00:00',
            'source_event_id': 'observed-fill',
        },
    }


@pytest.mark.parametrize('status', ['insufficient_data', 'disabled', 'degraded'])
def test_archived_analysis_caps_unsynchronized_fallback_without_enabling_ai(status):
    policy = derive_ai_execution_policy(
        _archived_state(status), requested=True, fallback_factor=Decimal('0.90'),
    )
    assert policy.effective is False
    assert policy.effective_mode == 'shadow'
    assert policy.factor == Decimal('0.70')
    assert policy.buffer_pct == Decimal('0.30')


def test_archive_never_relaxes_tighter_fallback_or_overrides_explicit_shadow():
    state = _archived_state('insufficient_data')
    state['last_valid_analysis']['analysis']['capital_policy']['buffer_pct'] = 0.10
    assert derive_ai_execution_policy(state, requested=True, fallback_factor=Decimal('0.70')).factor == Decimal('0.70')
    explicit_shadow = derive_ai_execution_policy(state, requested=False, fallback_factor=Decimal('0.70'))
    assert explicit_shadow.factor == Decimal('1')
    assert explicit_shadow.effective is False


def test_fresh_healthy_analysis_is_not_replaced_by_archived_policy():
    state = _archived_state('ok')
    state['analysis'] = {'capital_policy': {'buffer_pct': 0.05}}
    policy = derive_ai_execution_policy(state, requested=True, fallback_factor=Decimal('0.70'))
    assert policy.effective is True
    assert policy.factor == Decimal('0.95')


async def _run_ticks(monkeypatch, *, status, ticks, pending_at=None):
    """Drive the actual run() loop; substitute only time and external boundaries."""
    clock = SimpleNamespace(time=lambda: clock.now, now=0.0)
    stopped = SimpleNamespace(done=False)
    stopped.is_set = lambda: stopped.done
    monkeypatch.setattr(module, 'stop', stopped)
    monkeypatch.setattr(module, 'asyncio', SimpleNamespace(get_running_loop=lambda: clock))
    monkeypatch.setattr(module, '_env_bool', lambda *_args: True)
    monkeypatch.setattr(module, 'assert_schema', AsyncMock())

    @asynccontextmanager
    async def session():
        yield object()

    monkeypatch.setattr(module, 'SessionLocal', session)
    worker = module.AIIntelligenceWorker.__new__(module.AIIntelligenceWorker)
    worker.queue = 'test-events'
    worker.debounce = 0
    worker.min_refresh = 5
    worker.max_refresh = 600
    worker.failure_retry = 60
    worker.profit_exit_interval = 60
    worker._last_profit_exit_eval = 0
    worker._last_safety_check_mono = None
    worker._pending = False
    worker._last_signal_mono = None
    worker._source_ts = None
    worker._source_boundary_count = None
    worker._state = AsyncMock(return_value={
        'status': status, 'updated_at': '2000-01-01T00:00:00+00:00',
    })
    worker._latest_master_event = AsyncMock(return_value=None)
    worker._profit_exit_mode = AsyncMock(return_value=ProfitExitFeatureMode.OFF)
    worker._db_fallback_check = AsyncMock()
    worker._run_profit_exit_evaluation = AsyncMock()
    pending_ticks = list(ticks)
    refreshes = []

    async def consume():
        clock.now = pending_ticks.pop(0)
        if clock.now == pending_at:
            worker._pending = True
            worker._last_signal_mono = clock.now
        if not pending_ticks:
            stopped.done = True
        return False

    async def refresh(reason):
        refreshes.append((clock.now, reason))
        # Fingerprint cache hit: persisted updated_at deliberately unchanged.
        return True

    worker._consume_signal = consume
    worker._run_analysis = refresh
    await worker.run()
    worker._run_profit_exit_evaluation.assert_not_awaited()
    return refreshes


@pytest.mark.asyncio
async def test_run_loop_throttles_noop_checks_until_exact_deadline(monkeypatch):
    assert await _run_ticks(monkeypatch, status='ok', ticks=[1000, 1010, 1599, 1600]) == [
        (1000, 'safety_refresh'), (1600, 'safety_refresh'),
    ]


@pytest.mark.asyncio
async def test_run_loop_retries_degraded_at_failure_interval(monkeypatch):
    assert await _run_ticks(monkeypatch, status='degraded', ticks=[1000, 1010, 1059, 1060]) == [
        (1000, 'provider_retry'), (1060, 'provider_retry'),
    ]


@pytest.mark.asyncio
async def test_new_event_is_not_delayed_by_safety_refresh_throttle(monkeypatch):
    assert await _run_ticks(monkeypatch, status='ok', ticks=[1000, 1010, 1020], pending_at=1010) == [
        (1000, 'safety_refresh'), (1010, 'master_event_batch'),
    ]
