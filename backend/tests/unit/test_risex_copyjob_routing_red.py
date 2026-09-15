from __future__ import annotations

import importlib
import importlib.util
import inspect
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.models.entities import JobState
from app.services import queue as queue_service
from app.services import strategy_intents
from app.workers import execution_worker, resilient_execution_worker


class _CommitDB:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


@pytest.mark.asyncio
async def test_prepare_destination_allows_bound_risex_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _bound(_db, _job) -> bool:
        return True

    monkeypatch.setattr(queue_service, 'bind_job_to_active_destination', _bound)
    job = SimpleNamespace(
        execution_provider='risex',
        state=JobState.QUEUED,
        last_error=None,
        owner=None,
        locked_until=None,
        next_attempt_at=None,
        enqueued_at=None,
    )

    allowed = await queue_service.prepare_job_destination_for_execution(object(), job)

    assert allowed is True
    assert job.state == JobState.QUEUED
    assert job.last_error is None


@pytest.mark.asyncio
async def test_prepare_destination_still_rejects_unknown_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _bound(_db, _job) -> bool:
        return True

    monkeypatch.setattr(queue_service, 'bind_job_to_active_destination', _bound)
    job = SimpleNamespace(
        execution_provider='unknown-provider',
        state=JobState.QUEUED,
        last_error=None,
        owner='worker-before-guard',
        locked_until=datetime.now(),
        next_attempt_at=datetime.now(),
        enqueued_at=datetime.now(),
    )
    db = SimpleNamespace(flush=lambda: None)

    class _DB:
        async def flush(self) -> None:
            return None

    allowed = await queue_service.prepare_job_destination_for_execution(_DB(), job)

    assert allowed is False
    assert job.state == JobState.SKIPPED
    assert job.owner is None
    assert job.locked_until is None
    assert job.next_attempt_at is None
    assert job.enqueued_at is None


def test_all_four_delivery_callers_keep_the_shared_destination_guard() -> None:
    callers = {
        'publish_job': queue_service.publish_job,
        'repair_stream': queue_service.repair_stream,
        'worker_handle_job_id': execution_worker.Worker.handle_job_id,
        'postgres_fallback': resilient_execution_worker.ResilientExecutionWorker._next_database_job_id,
    }

    for name, caller in callers.items():
        source = inspect.getsource(caller)
        assert 'prepare_job_destination_for_execution(' in source, name
        assert "execution_provider != 'hyperliquid'" not in source, name


def test_worker_routes_risex_after_claim_without_changing_hyperliquid_branch_shape() -> None:
    source = inspect.getsource(execution_worker.Worker.handle_job_id)

    admin_shape = "elif job.origin=='ADMIN_LEVERAGE_SYNC':"
    hyperliquid_shape = 'result=await process_job(db,self.follower_hl(network),job)'
    risex_shape = "if job.execution_provider=='risex':"
    risex_call = 'result=await self._run_risex_copy_job(db,job)'

    assert admin_shape in source
    assert hyperliquid_shape in source
    assert risex_shape in source
    assert risex_call in source

    claim_index = source.index('job=await claim_job')
    risex_index = source.index(risex_shape)
    hyperliquid_index = source.index(hyperliquid_shape)
    assert claim_index < risex_index < hyperliquid_index


def test_worker_exposes_a_dedicated_risex_path_with_point_of_use_window_fence() -> None:
    runner = getattr(execution_worker.Worker, '_run_risex_copy_job', None)
    assert callable(runner), 'Step 4B requires a dedicated Worker._run_risex_copy_job path'

    source = inspect.getsource(runner)
    assert '_process_job_locked' not in source
    assert 'process_job(' not in source

    expire_index = source.index('self.risex_window.expire_if_needed()')
    enabled_index = source.index('RISExExecutionState.ENABLED', expire_index)
    per_operation_index = source.index('await process_risex_job(', enabled_index)
    assert expire_index < enabled_index < per_operation_index

    assert '_poll_risex_control_once' not in source
    assert 'WorkerHeartbeat' not in source
    assert 'build_risex_execution_status' not in source


@pytest.mark.asyncio
async def test_window_unavailable_deferral_preserves_failure_budget() -> None:
    defer = getattr(execution_worker, '_defer_risex_window_unavailable', None)
    assert callable(defer), 'Step 4B requires a dedicated post-claim RISEx window deferral'

    db = _CommitDB()
    job = SimpleNamespace(
        state=JobState.PROCESSING,
        attempt_count=4,
        last_error=None,
        owner='execution-worker-test',
        locked_until=datetime.now(),
        next_attempt_at=None,
        enqueued_at=datetime.now(),
    )

    result = await defer(db, job, window_state='PAUSED')

    assert result == JobState.RETRYING.value
    assert job.state == JobState.RETRYING
    assert job.attempt_count == 3, 'claim_job increment must be compensated by deferral'
    assert job.owner is None
    assert job.locked_until is None
    assert job.enqueued_at is None
    assert job.next_attempt_at is not None
    assert 'PAUSED' in (job.last_error or '')
    assert db.commits == 1


def test_latest_intent_authorization_becomes_provider_aware_for_risex_writer() -> None:
    signature = inspect.signature(strategy_intents.current_strategy_intent_for_cloid)
    assert 'execution_provider' in signature.parameters

    source = inspect.getsource(strategy_intents.current_strategy_intent_for_cloid)
    assert "job.execution_provider != 'hyperliquid'" not in source
    assert 'job.execution_provider != execution_provider' in source

    spec = importlib.util.find_spec('app.services.risex_copy_execution')
    assert spec is not None, 'Step 4B requires a dedicated RISEx CopyJob execution service'
    module = importlib.import_module('app.services.risex_copy_execution')
    process_risex_job = getattr(module, 'process_risex_job', None)
    assert callable(process_risex_job)

    writer_source = inspect.getsource(process_risex_job)
    assert 'current_strategy_intent_for_cloid(' in writer_source
    assert "execution_provider='risex'" in writer_source


def test_step_4b_explicitly_leaves_fresh_risex_operational_truth_for_step_4c() -> None:
    """Scope guard: 4B may submit RISEx orders, but reconciliation remains Hyperliquid-only."""

    reconcile_source = inspect.getsource(execution_worker.Worker.run_reconcile_if_leader)
    observability_source = inspect.getsource(execution_worker.Worker._refresh_follower_observability)

    assert 'follower_hl=self.follower_hl(network)' in reconcile_source
    assert 'resolve_ambiguous_executions(db,follower_hl)' in reconcile_source
    assert 'RISExAdapter' not in reconcile_source

    assert 'follower_hl=self.follower_hl(network)' in observability_source
    assert 'snapshot=await follower_hl.account_snapshot(account.account_address)' in observability_source
    assert 'RISExAdapter' not in observability_source
