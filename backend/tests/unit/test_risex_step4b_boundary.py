from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.models.entities import JobState
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


def _enabled_worker(fixed_now: datetime):
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
    return worker


def _patch_common(monkeypatch: pytest.MonkeyPatch) -> None:
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


def _job(worker, fixed_now):
    return SimpleNamespace(
        execution_provider='risex',
        execution_network='testnet',
        state=JobState.PROCESSING,
        last_error=None,
        owner=worker.id,
        locked_until=fixed_now,
        enqueued_at=fixed_now,
        attempt_count=1,
    )


@pytest.mark.asyncio
async def test_enabled_risex_worker_passes_exact_prepared_submission_and_request_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """b2 crosses the old Step-4B boundary with one exact process-local submission."""

    fixed_now = datetime(2026, 9, 21, 19, 0, tzinfo=UTC)
    worker = _enabled_worker(fixed_now)
    _patch_common(monkeypatch)

    transport = object()
    submission = object()
    prepared = SimpleNamespace(transport=transport, submission=submission)
    prepare_calls = 0

    async def prepare_once(*_args, **_kwargs):
        nonlocal prepare_calls
        prepare_calls += 1
        return prepared

    monkeypatch.setattr(
        execution_worker,
        'prepare_risex_worker_submission',
        prepare_once,
        raising=False,
    )

    adapter_transport = None

    class CapturingAdapter:
        def __init__(self, *, transport=None, **_kwargs):
            nonlocal adapter_transport
            adapter_transport = transport

    monkeypatch.setattr(execution_worker, 'RISExAdapter', CapturingAdapter)

    writer_calls = 0
    writer_submission = None

    async def counted_process_risex_job(_db, _adapter, _job, **kwargs):
        nonlocal writer_calls, writer_submission
        writer_calls += 1
        writer_submission = kwargs.get('submission')
        return JobState.DONE.value

    monkeypatch.setattr(execution_worker, 'process_risex_job', counted_process_risex_job)

    db = _CommitDB()
    result = await execution_worker.Worker._run_risex_copy_job(
        worker,
        db,
        _job(worker, fixed_now),
    )

    assert prepare_calls == 1
    assert adapter_transport is transport
    assert writer_calls == 1
    assert writer_submission is submission
    assert result == JobState.DONE.value


@pytest.mark.asyncio
async def test_risex_worker_preparation_failure_never_reaches_writer_or_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 9, 21, 19, 0, tzinfo=UTC)
    worker = _enabled_worker(fixed_now)
    _patch_common(monkeypatch)

    async def fail_preparation(*_args, **_kwargs):
        raise RuntimeError('b2 preparation failed before durable submission')

    monkeypatch.setattr(
        execution_worker,
        'prepare_risex_worker_submission',
        fail_preparation,
        raising=False,
    )

    writer_calls = 0

    async def unexpected_writer(*_args, **_kwargs):
        nonlocal writer_calls
        writer_calls += 1
        raise AssertionError('failed preparation must never reach process_risex_job')

    monkeypatch.setattr(execution_worker, 'process_risex_job', unexpected_writer)

    db = _CommitDB()
    job = _job(worker, fixed_now)
    result = await execution_worker.Worker._run_risex_copy_job(worker, db, job)

    assert writer_calls == 0
    assert result == JobState.RETRYING.value
    assert job.state == JobState.RETRYING
    assert 'prepar' in (job.last_error or '').lower()
