from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.admin import _require_follower_target
from app.api.user import _require_follower_user
from app.core.config import settings
from app.services.master_source_identity import (
    MASTER_SOURCE_FOLLOWER_BLOCK_REASON,
    is_master_source_user,
    is_master_source_wallet,
)
from app.services.reconcile import reconcile_user

MASTER = '0xaabbccddeeff00112233445566778899aabbccdd'
FOLLOWER = '0x1111111111111111111111111111111111111111'


def _user(wallet: str):
    return SimpleNamespace(auth_wallet=wallet, id='master-user')


def test_master_source_identity_is_central_and_case_insensitive(monkeypatch):
    monkeypatch.setattr(settings, 'HYPERLIQUID_MASTER_ADDRESS', MASTER)

    assert is_master_source_wallet(MASTER.upper().replace('0X', '0x')) is True
    assert is_master_source_user(_user(MASTER)) is True
    assert is_master_source_wallet(FOLLOWER) is False


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
