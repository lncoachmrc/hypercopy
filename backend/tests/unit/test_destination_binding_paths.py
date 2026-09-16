from __future__ import annotations

import inspect

from app.services import execution as execution_service
from app.services import queue as queue_service
from app.services import strategy_intents
from app.workers import execution_worker, resilient_execution_worker


def test_every_publish_and_database_fallback_path_requires_destination_binding() -> None:
    publish_source = inspect.getsource(queue_service.publish_job)
    repair_source = inspect.getsource(queue_service.repair_stream)
    fallback_source = inspect.getsource(resilient_execution_worker.ResilientExecutionWorker._next_database_job_id)

    assert 'prepare_job_destination_for_execution(db, job)' in publish_source
    assert 'prepare_job_destination_for_execution(db, job)' in repair_source
    assert 'prepare_job_destination_for_execution(db, job)' in fallback_source


def test_worker_rejects_unsupported_provider_before_claim_or_adapter_routing() -> None:
    guard_source = inspect.getsource(queue_service.prepare_job_destination_for_execution)
    worker_source = inspect.getsource(execution_worker.Worker.handle_job_id)

    assert "_SUPPORTED_EXECUTION_PROVIDERS = frozenset({'hyperliquid', 'risex'})" in inspect.getsource(queue_service)
    assert 'job.execution_provider not in _SUPPORTED_EXECUTION_PROVIDERS' in guard_source
    assert '_PROVIDER_WRITES_DISABLED_REASON' in guard_source
    assert queue_service._PROVIDER_WRITES_DISABLED_REASON == 'Execution provider is not enabled for writes'

    delivery_guard = 'prepare_job_destination_for_execution(db, raw)'
    assert delivery_guard in worker_source
    assert worker_source.index(delivery_guard) < worker_source.index('job=await claim_job')
    assert worker_source.index(delivery_guard) < worker_source.index("if job.execution_provider=='risex':")
    assert worker_source.index(delivery_guard) < worker_source.index('self.follower_hl(network)')


def test_worker_admits_bound_risex_but_routes_it_only_after_claim() -> None:
    guard_source = inspect.getsource(queue_service.prepare_job_destination_for_execution)
    worker_source = inspect.getsource(execution_worker.Worker.handle_job_id)

    assert "'risex'" in guard_source or '_SUPPORTED_EXECUTION_PROVIDERS' in guard_source
    assert "if job.execution_provider=='risex':" in worker_source
    assert 'result=await self._run_risex_copy_job(db,job)' in worker_source

    claim_index = worker_source.index('job=await claim_job')
    risex_index = worker_source.index("if job.execution_provider=='risex':")
    hyperliquid_index = worker_source.index('result=await process_job(db,self.follower_hl(network),job)')
    assert claim_index < risex_index < hyperliquid_index


def test_risex_execution_requires_enabled_process_local_window_or_defers() -> None:
    runner = getattr(execution_worker.Worker, '_run_risex_copy_job', None)
    defer = getattr(execution_worker, '_defer_risex_window_unavailable', None)
    assert callable(runner)
    assert callable(defer)

    runner_source = inspect.getsource(runner)
    defer_source = inspect.getsource(defer)
    assert 'self.risex_window.expire_if_needed()' in runner_source
    assert 'RISExExecutionState.ENABLED' in runner_source
    assert 'await _defer_risex_window_unavailable(' in runner_source
    assert 'attempt_count' in defer_source
    assert 'JobState.RETRYING' in defer_source
    assert 'WorkerHeartbeat' not in runner_source
    assert 'build_risex_execution_status' not in runner_source


def test_all_execution_delivery_paths_share_the_same_destination_guard() -> None:
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


def test_processing_rechecks_exact_epoch_after_claim() -> None:
    source = inspect.getsource(execution_service._process_job_locked)
    assert 'job_matches_active_destination(db, job)' in source
    assert 'Stale or unbound execution destination epoch' in source
    assert "job.execution_provider != 'hyperliquid'" in source


def test_execution_row_inherits_immutable_destination_binding() -> None:
    source = inspect.getsource(execution_service._execute_leg)
    assert 'execution_epoch_id=job.execution_epoch_id' in source
    assert 'execution_provider=job.execution_provider' in source
    assert 'execution_network=job.execution_network' in source


def test_normal_leverage_write_has_final_destination_authorization_callback() -> None:
    source = inspect.getsource(execution_service._process_job_locked)
    assert 'async def _authorize_destination_write()' in source
    assert 'before_submit=_authorize_destination_write' in source


def test_order_cloid_authorization_rechecks_job_and_execution_destination() -> None:
    source = inspect.getsource(strategy_intents.current_strategy_intent_for_cloid)
    assert 'job_matches_active_destination(db, job)' in source
    assert 'execution.execution_epoch_id != job.execution_epoch_id' in source
    assert 'execution.execution_provider != job.execution_provider' in source
    assert 'execution.execution_network != job.execution_network' in source


def test_admin_leverage_write_rechecks_exact_epoch_in_final_callback() -> None:
    source = inspect.getsource(execution_worker.Worker._run_admin_leverage_sync)
    assert 'job_matches_active_destination(db, job)' in source
    assert 'Stale or unbound execution destination epoch' in source
