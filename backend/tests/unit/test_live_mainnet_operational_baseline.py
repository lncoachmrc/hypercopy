from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import admin, auth
from app.api.admin import _position_config_sync_confirmation
from app.core.config import Settings


class _FlagDb:
    def __init__(self, enabled: dict[str, bool]):
        self.enabled = enabled

    async def get(self, _model: object, slug: str):
        return SimpleNamespace(enabled=self.enabled.get(slug, False))


def test_runtime_follower_fallback_defaults_to_mainnet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('HYPERLIQUID_NETWORK', raising=False)
    monkeypatch.delenv('HYPERLIQUID_FOLLOWER_NETWORK', raising=False)
    assert Settings.model_fields['HYPERLIQUID_NETWORK'].default == 'mainnet'
    cfg = Settings(_env_file=None)
    assert cfg.follower_network == 'mainnet'


def test_explicit_testnet_override_remains_available_for_local_and_test() -> None:
    cfg = Settings(
        _env_file=None,
        HYPERLIQUID_NETWORK='testnet',
        HYPERLIQUID_FOLLOWER_NETWORK='testnet',
    )
    assert cfg.follower_network == 'testnet'


def test_explicit_follower_network_override_wins_over_legacy_network() -> None:
    cfg = Settings(
        _env_file=None,
        HYPERLIQUID_NETWORK='testnet',
        HYPERLIQUID_FOLLOWER_NETWORK='mainnet',
    )
    assert cfg.follower_network == 'mainnet'


def test_new_user_onboarding_sets_configured_follower_network_explicitly() -> None:
    source = inspect.getsource(auth.verify)
    assert 'await set_user_network(db, user.id, settings.follower_network)' in source


def test_position_config_sync_confirmation_is_network_aware() -> None:
    assert _position_config_sync_confirmation('testnet') == 'SYNC TESTNET LEVERAGE'
    assert _position_config_sync_confirmation('mainnet') == 'SYNC MAINNET LEVERAGE'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('enabled', 'expected'),
    [
        ({}, []),
        ({'global_pause': True}, ['global_pause']),
        ({'emergency_stop': True}, ['emergency_stop']),
        ({'global_pause': True, 'emergency_stop': True}, ['global_pause', 'emergency_stop']),
    ],
)
async def test_active_system_execution_halts_detects_global_controls(
    enabled: dict[str, bool], expected: list[str]
) -> None:
    db = cast(AsyncSession, _FlagDb(enabled))
    assert await admin._active_system_execution_halts(db) == expected


def test_position_config_sync_has_no_testnet_only_block_and_keeps_live_safety_gates() -> None:
    source = inspect.getsource(admin.sync_position_config)
    assert 'restricted to TESTNET' not in source
    assert 'await live_trading_allowed(db, network)' in source
    assert 'await _active_system_execution_halts(db)' in source
    assert 'target.copy_state != CopyState.PAUSED' in source
    assert "diagnostic['allowed_asset']" in source
    assert '_credential_active(cred)' in source
