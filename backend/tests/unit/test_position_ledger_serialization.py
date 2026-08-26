import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.db.position_ledger_lock import _lock_id
from app.services import execution, reconcile


def test_position_ledger_lock_id_is_stable_and_user_scoped():
    first = uuid.uuid4()
    second = uuid.uuid4()

    assert _lock_id(first) == _lock_id(str(first))
    assert _lock_id(first) != _lock_id(second)
    assert -(2**63) <= _lock_id(first) < 2**63


@pytest.mark.asyncio
async def test_process_job_holds_user_lock_around_execution(monkeypatch):
    user_id = uuid.uuid4()
    events = []

    @asynccontextmanager
    async def fake_lock(received_user_id):
        events.append(("lock_enter", received_user_id))
        yield
        events.append(("lock_exit", received_user_id))

    async def fake_process(_db, _hl, _job):
        events.append(("process", user_id))
        return "DONE"

    monkeypatch.setattr(execution, "position_ledger_lock", fake_lock)
    monkeypatch.setattr(execution, "_process_job_locked", fake_process)

    result = await execution.process_job(object(), object(), SimpleNamespace(user_id=user_id))

    assert result == "DONE"
    assert events == [
        ("lock_enter", user_id),
        ("process", user_id),
        ("lock_exit", user_id),
    ]


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
