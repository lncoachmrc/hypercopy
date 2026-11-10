"""RISEx worker must never submit an EVENT/RECONCILE under SHADOW or PAUSED."""
from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace

import pytest

from app.models.entities import CopyState, JobState, User, UserState
from app.workers import execution_worker


class MiniDB:
    def __init__(self, state):
        self.state=state
        self.commits=0
        self.retrieved=0
    async def get(self, model, uid):
        assert model is User
        self.retrieved+=1
        return SimpleNamespace(copy_state=self.state, state=UserState.ACTIVE) if self.state else None
    async def commit(self):
        self.commits+=1


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [CopyState.SHADOW, CopyState.PAUSED, None])
async def test_risex_worker_skips_new_source_jobs_without_potential_to_submit(monkeypatch,state):
    # A fake DB without execute skips CASE-A provider-truth recovery in this
    # unit test; production recovery always precedes the new-submit guard.
    db=MiniDB(state)
    job=SimpleNamespace(
        user_id=uuid.uuid4(),execution_provider="risex",
        execution_network="testnet",origin="EVENT",
        state=JobState.PROCESSING,last_error=None,
        owner="worker",locked_until=None,next_attempt_at=None,enqueued_at=None,
    )
    worker=execution_worker.Worker.__new__(execution_worker.Worker)
    class ForbiddenWindow:
        def expire_if_needed(self):
            raise AssertionError("Window and all provider IO must be unreachable")
    worker.risex_window=ForbiddenWindow()
    result=await worker._run_risex_copy_job(db,job)
    assert result==JobState.SKIPPED.value
    assert job.state is JobState.SKIPPED
    assert job.owner is None
    assert db.commits==1


def test_recovery_precedes_new_submission_state_guard():
    source=inspect.getsource(execution_worker.Worker._run_risex_copy_job)
    assert source.index("await recover_risex_case_a_job(") < source.index(
        "if getattr(job, 'origin', None) in {'EVENT', 'RECONCILE'}:"
    )
    assert source.index("if getattr(job, 'origin', None) in {'EVENT', 'RECONCILE'}:") < source.index(
        "self.risex_window.expire_if_needed()"
    )
