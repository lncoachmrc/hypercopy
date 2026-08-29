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


def test_position_config_sync_authorizes_inside_signed_submission_path() -> None:
    source = inspect.getsource(admin.sync_position_config)
    assert 'restricted to TESTNET' not in source
    assert source.count('_follower_adapter(network)') == 1
    assert 'await live_trading_allowed(db, network)' not in source
    assert '_active_system_execution_halts' not in source
    assert "if not diagnostic['allowed_asset']" not in source
    assert 'follower_hl=follower_hl' in source
    assert 'await _position_config_sync_signing_material(db, target)' in source
    assert 'async def _authorize_submission()' in source
    assert 'await _fresh_position_config_sync_authorization(' in source
    assert 'before_submit=_authorize_submission' in source
    assert 'except HTTPException:' in source

    metadata_index = source.index('await follower_hl.asset_spec(asset)')
    material_index = source.index('await _position_config_sync_signing_material')
    decrypt_index = source.index('private_key = crypto.decrypt')
    signed_update_index = source.index('response = await follower_hl.update_leverage')
    assert metadata_index < material_index < decrypt_index < signed_update_index


def test_final_sync_authorization_revalidates_controls_risk_and_signing_identity() -> None:
    source = inspect.getsource(admin._fresh_position_config_sync_authorization)
    assert 'current_network = (await user_network_state(db, target.id)).network' in source
    assert 'db.expire_all()' not in source
    assert "await db.refresh(target, attribute_names=['copy_state'])" in source
    assert 'target.copy_state != CopyState.PAUSED' in source
    assert "SystemFlag.slug.in_(('live_trading', 'global_pause', 'emergency_stop'))" in source
    assert "expected_network == 'mainnet'" in source
    assert 'select(RiskProfile)' in source
    assert 'allowed_asset =' in source
    assert 'desired_leverage = max(1, min(master_leverage, int(risk.max_leverage), exchange_max_leverage))' in source
    assert 'select(TradingAccount)' in source
    assert 'account.id != expected_account_id' in source
    assert 'account.account_address.lower() != expected_account_address.lower()' in source
    assert 'select(SigningCredential)' in source
    assert 'cred.id != expected_credential_id' in source
    assert '_credential_active(cred)' in source
    assert source.count('execution_options(populate_existing=True)') >= 4
    assert 'return desired_leverage, desired_is_cross' in source


def test_signing_material_is_prerequisite_not_policy_gate() -> None:
    source = inspect.getsource(admin._position_config_sync_signing_material)
    assert 'select(TradingAccount)' in source
    assert 'select(SigningCredential)' in source
    assert '_credential_active(cred)' in source
    assert 'RiskProfile' not in source
    assert 'SystemFlag' not in source
    assert 'copy_state' not in source


def test_diagnostic_can_reuse_preloaded_follower_adapter() -> None:
    source = inspect.getsource(admin._position_config_diagnostic)
    assert 'follower_hl: HyperliquidAdapter | None = None' in source
    assert 'follower_hl = follower_hl or _follower_adapter(network)' in source
    assert 'follower_hl.network != network' in source


def test_verification_read_failure_is_audited_before_502() -> None:
    source = inspect.getsource(admin.sync_position_config)
    verification_start = source.index(
        'follower_state = await follower_hl.user_state(account_address, priority=Priority.DIAGNOSTIC)'
    )
    verification_error = source.index("verification_error = f'{type(exc).__name__}: {exc}'", verification_start)
    audit_index = source.index("action='ADMIN_FOLLOWER_LEVERAGE_SYNC_UNVERIFIED'", verification_error)
    commit_index = source.index('await db.commit()', audit_index)
    raise_index = source.index("raise HTTPException(502, f'Leverage update sent, but verification read failed", commit_index)

    assert verification_start < verification_error < audit_index < commit_index < raise_index
    failure_block = source[verification_error:raise_index]
    assert "'response': response" in failure_block
    assert "'desired': desired" in failure_block
    assert "'observed': None" in failure_block
    assert "'verification_error': verification_error" in failure_block
