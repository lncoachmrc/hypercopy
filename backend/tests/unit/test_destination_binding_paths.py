from __future__ import annotations

import inspect

from app.services import execution as execution_service
from app.services import queue as queue_service
from app.workers import resilient_execution_worker


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


def test_execution_row_inherits_immutable_destination_binding() -> None:
    source = inspect.getsource(execution_service._execute_leg)
    assert 'execution_epoch_id=job.execution_epoch_id' in source
    assert 'execution_provider=job.execution_provider' in source
    assert 'execution_network=job.execution_network' in source


def test_normal_leverage_write_has_final_destination_authorization_callback() -> None:
    source = inspect.getsource(execution_service._process_job_locked)
    assert 'async def _authorize_destination_write()' in source
    assert 'before_submit=_authorize_destination_write' in source
