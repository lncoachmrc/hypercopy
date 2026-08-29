from __future__ import annotations

import inspect

import pytest

from app.api import admin, auth
from app.api.admin import _position_config_sync_confirmation
from app.core.config import Settings


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


def test_position_config_sync_uses_one_fresh_pre_sign_authorization() -> None:
    source = inspect.getsource(admin.sync_position_config)
    assert 'restricted to TESTNET' not in source
    assert source.count('await _fresh_position_config_sync_authorization(db, target, network)') == 1
    assert 'await live_trading_allowed(db, network)' not in source
    assert '_active_system_execution_halts' not in source
    assert "diagnostic['allowed_asset']" in source

    diagnostic_index = source.index('diagnostic = await _position_config_diagnostic')
    guard_index = source.index('await _fresh_position_config_sync_authorization')
    decrypt_index = source.index('private_key = crypto.decrypt')
    signed_update_index = source.index('response = await follower_hl.update_leverage')
    assert diagnostic_index < guard_index < decrypt_index < signed_update_index


def test_final_sync_authorization_refreshes_controls_and_signing_material() -> None:
    source = inspect.getsource(admin._fresh_position_config_sync_authorization)
    assert 'current_network = (await user_network_state(db, target.id)).network' in source
    assert 'db.expire_all()' in source
    assert "await db.refresh(target, attribute_names=['copy_state'])" in source
    assert 'target.copy_state != CopyState.PAUSED' in source
    assert 'await live_trading_allowed(db, expected_network)' in source
    assert "SystemFlag.slug.in_(('global_pause', 'emergency_stop'))" in source
    assert 'select(TradingAccount)' in source
    assert 'select(SigningCredential)' in source
    assert '_credential_active(cred)' in source
    assert source.count('execution_options(populate_existing=True)') >= 3
    assert 'return account.id, account.account_address, cred' in source
