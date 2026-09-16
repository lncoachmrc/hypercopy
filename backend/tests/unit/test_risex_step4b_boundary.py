from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.adapters.risex import RISExAdapter
from app.models.entities import JobState
from app.services.risex_copy_execution import process_risex_job as real_process_risex_job
from app.services.risex_execution_window import (
    RISExExecutionState,
    RISExOperationalWindowController,
)
from app.workers import execution_worker


ACCOUNT = '0x' + ('11' * 20)
SIGNER = '0x' + ('22' * 20)
CONTEXT_FINGERPRINT = 'risex-step4b-boundary-context'


class _CommitDB:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


@pytest.mark.asyncio
async def test_enabled_risex_worker_defers_without_prepared_submission_and_never_posts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 4B ends at routing + continuous authorization, before submission preparation.

    With an ENABLED operational window and all point-of-use authorization fences
    satisfied, the worker reaches ``process_risex_job`` without a
    ``RISExPreparedCopySubmission``. The dedicated writer must therefore return
    RETRYING and no signed provider POST may be attempted. Preparing that
    process-local submission is intentionally left to the dedicated follow-up PR.
    """

    fixed_now = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
    worker = execution_worker.Worker.__new__(execution_worker.Worker)
    worker.id = 'execution-worker-test'
    worker.boot_id = uuid.uuid4()
    worker.risex_submission_lock = asyncio.Lock()
    worker.risex_window = RISExOperationalWindowController(
        worker_id=worker.id,
        boot_id=worker.boot_id,
        clock=lambda: fixed_now,
    )

    request_id = uuid.uuid4()
    assert worker.risex_window.begin_arm(
        request_id=request_id,
        control_generation=1,
        context_fingerprint=CONTEXT_FINGERPRINT,
    )
    worker.risex_window.open_window(
        request_id=request_id,
        control_generation=1,
        context_fingerprint=CONTEXT_FINGERPRINT,
    )
    assert worker.risex_window.state == RISExExecutionState.ENABLED

    monkeypatch.setattr(
        execution_worker,
        'load_testnet_signer_credential',
        lambda _env: SimpleNamespace(
            account_address=ACCOUNT,
            signer_address=SIGNER,
        ),
    )
    monkeypatch.setattr(
        execution_worker,
        'current_risex_context_fingerprint',
        lambda _worker, _assertions: CONTEXT_FINGERPRINT,
    )

    async def singleton_ok(_worker: object, _db: object) -> bool:
        return True

    monkeypatch.setattr(execution_worker, 'risex_singleton_matches_worker', singleton_ok)

    writer_calls = 0

    async def counted_process_risex_job(db, adapter, job, **kwargs):
        nonlocal writer_calls
        writer_calls += 1
        assert kwargs.get('submission') is None
        return await real_process_risex_job(db, adapter, job, **kwargs)

    monkeypatch.setattr(execution_worker, 'process_risex_job', counted_process_risex_job)

    post_calls = 0

    async def unexpected_place_ioc(self, **_kwargs):
        nonlocal post_calls
        post_calls += 1
        raise AssertionError('Step 4B must not enter the signed POST path without submission material')

    monkeypatch.setattr(RISExAdapter, 'place_ioc', unexpected_place_ioc)

    db = _CommitDB()
    job = SimpleNamespace(
        execution_provider='risex',
        execution_network='testnet',
        state=JobState.PROCESSING,
        last_error=None,
        owner=worker.id,
        locked_until=fixed_now,
        enqueued_at=fixed_now,
    )

    result = await execution_worker.Worker._run_risex_copy_job(worker, db, job)

    assert writer_calls == 1
    assert result == JobState.RETRYING.value
    assert job.state == JobState.RETRYING
    assert 'prepared process-local submission is unavailable' in (job.last_error or '')
    assert job.owner is None
    assert job.locked_until is None
    assert job.enqueued_at is None
    assert db.commits == 1
    assert post_calls == 0
    assert worker.risex_window.state == RISExExecutionState.ENABLED
