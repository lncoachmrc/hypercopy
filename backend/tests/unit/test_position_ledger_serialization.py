import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.db.position_ledger_lock import _lock_id, position_ledger_lock_engine
from app.db.session import engine
from app.models.entities import JobState
from app.services import execution, reconcile


def test_position_ledger_lock_id_is_stable_and_user_scoped():
    first = uuid.uuid4()
    second = uuid.uuid4()

    assert _lock_id(first) == _lock_id(str(first))
    assert _lock_id(first) != _lock_id(second)
    assert -(2**63) <= _lock_id(first) < 2**63


def test_position_ledger_lock_uses_a_separate_engine():
    assert position_ledger_lock_engine is not engine


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Db:
    def __init__(self, value):
        self.value = value
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, _statement):
        return _Result(self.value)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_lease_is_revalidated_and_renewed_after_lock_wait():
    previous_expiry = datetime.now(UTC)
    job = SimpleNamespace(
        id=uuid.uuid4(),
        state=JobState.PROCESSING,
        owner="worker-1",
        locked_until=previous_expiry,
    )
    db = _Db(job)

    result = await execution._renew_job_lease_after_lock(db, job.id, "worker-1")

    assert result is job
    assert job.locked_until > previous_expiry
    assert db.commits == 1
    assert db.rollbacks == 0


@pytest.mark.asyncio
async def test_lease_revalidation_rejects_a_new_owner():
    job = SimpleNamespace(
        id=uuid.uuid4(),
        state=JobState.PROCESSING,
        owner="worker-2",
        locked_until=datetime.now(UTC),
    )
    db = _Db(job)

    result = await execution._renew_job_lease_after_lock(db, job.id, "worker-1")

    assert result is None
    assert db.commits == 0
    assert db.rollbacks == 1


@pytest.mark.asyncio
async def test_process_job_holds_user_lock_around_execution(monkeypatch):
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    owner = "worker-1"
    job = SimpleNamespace(id=job_id, user_id=user_id, owner=owner)
    events = []

    @asynccontextmanager
    async def fake_lock(received_user_id):
        events.append(("lock_enter", received_user_id))
        yield
        events.append(("lock_exit", received_user_id))

    async def fake_process(_db, _hl, _job):
        events.append(("process", user_id))
        return "DONE"

    async def fake_renew(_db, received_job_id, received_owner):
        events.append(("renew", received_job_id, received_owner))
        return job

    monkeypatch.setattr(execution, "position_ledger_lock", fake_lock)
    monkeypatch.setattr(execution, "_renew_job_lease_after_lock", fake_renew)
    monkeypatch.setattr(execution, "_process_job_locked", fake_process)

    result = await execution.process_job(object(), object(), job)

    assert result == "DONE"
    assert events == [
        ("lock_enter", user_id),
        ("renew", job_id, owner),
        ("process", user_id),
        ("lock_exit", user_id),
    ]


@pytest.mark.asyncio
async def test_process_job_stops_if_lease_was_reassigned_while_waiting(monkeypatch):
    user_id = uuid.uuid4()
    job = SimpleNamespace(id=uuid.uuid4(), user_id=user_id, owner="worker-1")
    processed = False

    @asynccontextmanager
    async def fake_lock(_user_id):
        yield

    async def fake_renew(_db, _job_id, _owner):
        return None

    async def fake_process(_db, _hl, _job):
        nonlocal processed
        processed = True

    monkeypatch.setattr(execution, "position_ledger_lock", fake_lock)
    monkeypatch.setattr(execution, "_renew_job_lease_after_lock", fake_renew)
    monkeypatch.setattr(execution, "_process_job_locked", fake_process)

    result = await execution.process_job(object(), object(), job)

    assert result == JobState.RETRYING.value
    assert processed is False


@pytest.mark.asyncio
async def test_reconcile_user_holds_same_user_lock(monkeypatch):
    user_id = uuid.uuid4()
    user = SimpleNamespace(id=user_id)
    events = []

    @asynccontextmanager
    async def fake_lock(received_user_id):
        events.append(("lock_enter", received_user_id))
        yield
        events.append(("lock_exit", received_user_id))

    async def fake_reconcile(_db, _hl, _user, **_kwargs):
        events.append(("reconcile", user_id))
        return {"status": "OK"}

    monkeypatch.setattr(reconcile, "position_ledger_lock", fake_lock)
    monkeypatch.setattr(reconcile, "_reconcile_user_locked", fake_reconcile)

    result = await reconcile.reconcile_user(
        object(),
        object(),
        user,
        master_positions={},
        master_equity=0,
        mids={},
    )

    assert result == {"status": "OK"}
    assert events == [
        ("lock_enter", user_id),
        ("reconcile", user_id),
        ("lock_exit", user_id),
    ]
