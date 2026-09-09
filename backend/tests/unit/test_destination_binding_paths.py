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
