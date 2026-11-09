"""Real PostgreSQL contracts for PR 297; temporary tables, no live services."""
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.session import engine
from app.models.entities import MasterEvent, SystemFlag
from app.services import ai_intelligence as intelligence
from app.services.ai_mode import AI_EXECUTION_FLAG_SLUG, read_ai_execution_policy
from app.workers import ai_intelligence_worker as worker_module

pytestmark = pytest.mark.skipif(os.getenv('RUN_INTEGRATION') != '1', reason='requires CI PostgreSQL')


@pytest_asyncio.fixture
async def database(monkeypatch):
    # LIKE preserves real columns/indexes. pg_temp shadows only these tables on
    # this connection; the outer rollback removes all fixtures, including commits
    # performed by the production service inside nested savepoints.
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(text('CREATE TEMP TABLE master_events (LIKE public.master_events INCLUDING ALL) ON COMMIT DROP'))
            await connection.execute(text('CREATE TEMP TABLE system_flags (LIKE public.system_flags INCLUDING ALL) ON COMMIT DROP'))
            factory = async_sessionmaker(connection, expire_on_commit=False, join_transaction_mode='create_savepoint')
            monkeypatch.setattr(worker_module, 'SessionLocal', factory)
            monkeypatch.setenv('LLM_ENABLED', 'true')
            monkeypatch.setenv('LLM_MODE', 'shadow')
            monkeypatch.setenv('LLM_LOOKBACK_DAYS', '30')
            calls = []

            async def provider(system, prompt):
                calls.append((system, prompt))
                return {'summary': 'Test evidence', 'capital_policy': {'buffer_pct': 0.30}, 'confidence': 0.8}, {
                    'provider': 'openai', 'model': 'test-only', 'fallback_index': 0, 'failures': [],
                }

            monkeypatch.setattr(intelligence, 'call_llm_with_failover', provider)
            yield factory, calls
        finally:
            await transaction.rollback()
    await engine.dispose()


def _event(identity, stamp):
    return MasterEvent(exchange_event_id=identity, event_ts=stamp, asset='BTC', side='B',
        size=Decimal('1'), price=Decimal('100'), start_position=Decimal('0'),
        position_after=Decimal('1'), master_equity=Decimal('1000'), raw={}, fencing_token=1)


@pytest.mark.parametrize('new_id', ['a-lower-id', 'z-higher-id'])
@pytest.mark.asyncio
async def test_postgres_restart_and_fallback_detect_equal_timestamp_new_fill(database, new_id):
    factory, calls = database
    stamp = datetime.now(UTC) - timedelta(minutes=1)
    async with factory() as db:
        db.add(_event('m-observed-id', stamp))
        await db.commit()
        first = await intelligence.refresh_ai_intelligence(db, force=True)
        assert first.get('observed_source_boundary_count') == 1
        db.add(_event(new_id, stamp))
        await db.commit()

    worker = worker_module.AIIntelligenceWorker.__new__(worker_module.AIIntelligenceWorker)
    await worker._sync_source_from_state()
    assert await worker._has_unanalysed_events() is True
    worker._pending = False
    worker._last_signal_mono = None
    worker._last_db_poll = float('-inf')
    worker.db_poll = 60
    worker.debounce = 0
    await worker._db_fallback_check()
    assert worker._pending is True

    async with factory() as db:
        second = await intelligence.refresh_ai_intelligence(db, force=True)
        assert second.get('observed_source_boundary_count') == 2
        assert second['strategy_profile']['lookback_events'] == 2
    await worker._sync_source_from_state()
    assert await worker._has_unanalysed_events() is False
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_cache_hit_repairs_older_cursor_without_billing_or_freshening_analysis(database):
    factory, calls = database
    stamp = datetime.now(UTC) - timedelta(minutes=1)
    async with factory() as db:
        db.add_all([_event('a-fill', stamp), _event('m-fill', stamp)])
        await db.commit()
        healthy = await intelligence.refresh_ai_intelligence(db, force=True)
        # Arrival between cursor and input reads: hash includes it, cursor lags.
        row = await db.get(SystemFlag, intelligence.AI_FLAG_SLUG)
        row.value = {**healthy, 'observed_source_boundary_count': 1}
        await db.commit()
        repaired = await intelligence.refresh_ai_intelligence(db, force=True)
        assert repaired.get('observed_source_boundary_count') == 2
        assert repaired['updated_at'] == healthy['updated_at']
        assert repaired['input_fingerprint'] == healthy['input_fingerprint']
    async with factory() as db:
        saved = await intelligence.read_ai_intelligence(db)
        assert saved['observed_source_boundary_count'] == 2
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_expired_analysis_freezes_and_persists_stricter_cap_across_sessions(database, monkeypatch):
    factory, calls = database
    async with factory() as db:
        db.add(_event('old-event', datetime.now(UTC) - timedelta(days=20)))
        await db.commit()
        healthy = await intelligence.refresh_ai_intelligence(db, force=True)
        assert healthy['status'] == 'ok'
        db.add(SystemFlag(slug=AI_EXECUTION_FLAG_SLUG, enabled=True, value={
            'requested_mode': 'on', 'last_valid_factor': '0.90', 'last_valid_buffer_pct': '0.10',
        }))
        await db.commit()
        monkeypatch.setenv('LLM_LOOKBACK_DAYS', '1')
        expired = await intelligence.refresh_ai_intelligence(db, force=True)
        assert expired['status'] == 'insufficient_data'
        assert expired['analysis'] is None
        policy = await read_ai_execution_policy(db)
        assert policy.effective is False
        assert policy.factor == Decimal('0.70')
        await db.commit()
    async with factory() as db:
        mode = await db.get(SystemFlag, AI_EXECUTION_FLAG_SLUG)
        assert Decimal(mode.value['last_valid_factor']) == Decimal('0.70')
        policy = await read_ai_execution_policy(db)
        assert policy.factor == Decimal('0.70')
        assert policy.effective is False
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_changed_template_is_used_by_provider_not_only_by_fingerprint(database, monkeypatch):
    factory, calls = database
    async with factory() as db:
        db.add(_event('one', datetime.now(UTC) - timedelta(minutes=1)))
        await db.commit()
        await intelligence.refresh_ai_intelligence(db, force=True)
        monkeypatch.setattr(intelligence, 'CAPITAL_INTELLIGENCE_SYSTEM_PROMPT', 'Changed test system template', raising=False)
        await intelligence.refresh_ai_intelligence(db, force=True)
        assert len(calls) == 2
        assert calls[-1][0] == 'Changed test system template'


@pytest.mark.asyncio
async def test_event_arriving_during_analysis_is_not_acknowledged_early(database, monkeypatch):
    factory, calls = database
    stamp = datetime.now(UTC) - timedelta(minutes=1)
    original_provider = intelligence.call_llm_with_failover
    async with factory() as db:
        db.add(_event('m-original', stamp))
        await db.commit()

        async def provider_with_arrival(system, prompt):
            db.add(_event('a-arrived-during-call', stamp))
            await db.flush()
            return await original_provider(system, prompt)

        monkeypatch.setattr(intelligence, 'call_llm_with_failover', provider_with_arrival)
        state = await intelligence.refresh_ai_intelligence(db, force=True)
        assert state.get('observed_source_boundary_count') == 1
        assert state['strategy_profile']['lookback_events'] == 1
    worker = worker_module.AIIntelligenceWorker.__new__(worker_module.AIIntelligenceWorker)
    await worker._sync_source_from_state()
    assert await worker._has_unanalysed_events() is True
    assert len(calls) == 1
