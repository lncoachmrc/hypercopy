from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.admin import _require_follower_target
from app.api.user import _require_follower_user
from app.core.config import settings
from app.models.entities import JobState, User
from app.services.master_source_identity import (
    MASTER_SOURCE_FOLLOWER_BLOCK_REASON,
    is_master_source_user,
    is_master_source_wallet,
)
from app.services.queue import publish_job
from app.services.reconcile import reconcile_user

MASTER = '0xaabbccddeeff00112233445566778899aabbccdd'
FOLLOWER = '0x1111111111111111111111111111111111111111'


def _user(wallet: str):
    return SimpleNamespace(auth_wallet=wallet, id=uuid.uuid4())


def test_master_source_identity_is_central_and_case_insensitive(monkeypatch):
    monkeypatch.setattr(settings, 'HYPERLIQUID_MASTER_ADDRESS', MASTER)

    assert is_master_source_wallet(MASTER.upper().replace('0X', '0x')) is True
    assert is_master_source_user(_user(MASTER)) is True
    assert is_master_source_wallet(FOLLOWER) is False
    assert is_master_source_user(SimpleNamespace(id=uuid.uuid4())) is False


def test_admin_and_user_follower_controls_reject_master(monkeypatch):
    monkeypatch.setattr(settings, 'HYPERLIQUID_MASTER_ADDRESS', MASTER)
    master = _user(MASTER)

    for guard in (_require_follower_target, _require_follower_user):
        with pytest.raises(HTTPException) as exc:
            guard(master)
        assert exc.value.status_code == 409
        assert exc.value.detail == MASTER_SOURCE_FOLLOWER_BLOCK_REASON


@pytest.mark.asyncio
async def test_reconcile_user_skips_master_before_any_follower_io(monkeypatch):
    monkeypatch.setattr(settings, 'HYPERLIQUID_MASTER_ADDRESS', MASTER)
    master = _user(MASTER)

    result = await reconcile_user(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        master,  # type: ignore[arg-type]
        master_positions={},
        master_equity=0,  # type: ignore[arg-type]
        mids={},
    )

    assert result == {
        'status': 'SKIPPED_MASTER_SOURCE',
        'network': 'mainnet',
        'jobs_created': 0,
    }


class _PublishDb:
    def __init__(self, user):
        self.user = user
        self.flushed = False

    async def get(self, model, _key):
        assert model is User
        return self.user

    async def flush(self):
        self.flushed = True


class _NoRedisPublish:
    async def xadd(self, *_args, **_kwargs):
        raise AssertionError('Master Source job must never be published to Redis')


@pytest.mark.asyncio
async def test_publish_job_terminally_skips_master_before_redis(monkeypatch):
    monkeypatch.setattr(settings, 'HYPERLIQUID_MASTER_ADDRESS', MASTER)
    master = _user(MASTER)
    db = _PublishDb(master)
    job = SimpleNamespace(
        user_id=master.id,
        state=JobState.QUEUED,
        last_error=None,
        owner='worker',
        locked_until=object(),
        next_attempt_at=object(),
        enqueued_at=object(),
    )

    await publish_job(_NoRedisPublish(), db, job)  # type: ignore[arg-type]

    assert job.state == JobState.SKIPPED
    assert job.last_error == MASTER_SOURCE_FOLLOWER_BLOCK_REASON
    assert job.owner is None
    assert job.locked_until is None
    assert job.next_attempt_at is None
    assert job.enqueued_at is None
    assert db.flushed is True
