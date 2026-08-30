from __future__ import annotations

import inspect

import pytest

from app.adapters.address_ratelimit import AddressActionTracker
from app.api import admin, auth
from app.api.admin import _position_config_sync_confirmation
from app.core.config import Settings
from app.services.admin_leverage_sync import fresh_position_config_sync_authorization
from app.workers import execution_worker


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


def test_position_config_sync_delegates_signing_to_execution_worker() -> None:
    source = inspect.getsource(admin.sync_position_config)
    assert '_position_config_sync_signing_material' in source
    assert '_queue_position_config_sync_job(' in source
    assert '_wait_position_config_sync_job(' in source
    assert 'crypto.decrypt' not in source
    assert '.update_leverage(' not in source
    assert "origin='ADMIN_LEVERAGE_SYNC'" in inspect.getsource(admin._queue_position_config_sync_job)


def test_api_module_has_no_credential_decrypt_path() -> None:
    source = inspect.getsource(admin)
    assert 'crypto.decrypt' not in source
    assert '_credential_blob' not in source


def test_worker_signed_sync_authorizes_inside_signed_submission_path() -> None:
    source = inspect.getsource(execution_worker.Worker._run_admin_leverage_sync)
    assert 'signing_secret = crypto.decrypt(' in source
    assert 'async def _authorize_submission()' in source
    assert 'fresh_master_hl = HyperliquidAdapter(' in source
    assert 'fresh_spec_hl = HyperliquidAdapter(' in source
    assert 'priority=Priority.ORDER' in source
    assert 'reestablish_submission_slot_before_final_authorization(' in source
    assert 'await fresh_position_config_sync_authorization(' in source
    assert 'before_submit=_authorize_submission' in source
    assert 'response = await follower_hl.update_leverage(' in source

    decrypt_index = source.index('signing_secret = crypto.decrypt(')
    callback_index = source.index('async def _authorize_submission()')
    signed_update_index = source.index('response = await follower_hl.update_leverage(')
    assert decrypt_index < callback_index < signed_update_index


def test_worker_routes_admin_leverage_jobs_away_from_generic_copy_execution() -> None:
    source = inspect.getsource(execution_worker.Worker.handle_job_id)
    assert "elif job.origin=='ADMIN_LEVERAGE_SYNC':" in source
    assert 'result=await self._run_admin_leverage_sync(db,job)' in source


def test_stale_signed_admin_leverage_action_is_quarantined_not_retried() -> None:
    source = inspect.getsource(execution_worker._quarantine_stale_admin_leverage_jobs)
    assert "CopyJob.origin == 'ADMIN_LEVERAGE_SYNC'" in source
    assert 'CopyJob.state == JobState.PROCESSING' in source
    assert "ctx['submission_status'] = 'UNKNOWN_AFTER_WORKER_LOSS'" in source
    assert 'job.state = JobState.DEAD' in source
    assert 'JobState.RETRYING' not in source
    maintenance = inspect.getsource(execution_worker.Worker.maintenance)
    quarantine_index = maintenance.index('await _quarantine_stale_admin_leverage_jobs(db)')
    generic_release_index = maintenance.index('await release_stale_jobs(db)')
    assert quarantine_index < generic_release_index


def test_final_sync_authorization_is_one_db_snapshot_of_all_mutable_controls() -> None:
    source = inspect.getsource(fresh_position_config_sync_authorization)
    assert source.count('await db.execute(') == 1
    assert 'user_network_state(' not in source
    assert 'db.refresh(' not in source
    assert 'execution_options(populate_existing=True)' not in source
    assert 'FROM users u' in source
    assert 'LEFT JOIN risk_profiles r ON r.user_id = u.id' in source
    assert 'LEFT JOIN trading_accounts ta ON ta.user_id = u.id' in source
    assert 'LEFT JOIN signing_credentials sc ON sc.trading_account_id = ta.id' in source
    assert "slug = 'live_trading'" in source
    assert "slug = 'global_pause'" in source
    assert "slug = 'emergency_stop'" in source
    assert "str(row['copy_state'] or '') != CopyState.PAUSED.value" in source
    assert "row['risk_max_leverage'] is None" in source
    assert "risk_max_leverage = Decimal(str(row['risk_max_leverage']))" in source
    assert 'allowed_asset =' in source
    assert "account_id != expected_account_id" in source
    assert 'account_address.lower() != expected_account_address.lower()' in source
    assert "row['credential_id'] != expected_credential_id" in source
    assert 'CredentialStatus.ACTIVE.value' in source
    assert 'CredentialStatus.EXPIRING.value' in source
    assert "expected_network == 'mainnet'" in source
    assert "bool(row['live_trading'])" in source
    assert "bool(row[slug])" in source
    assert 'return desired_leverage, desired_is_cross, str(risk_max_leverage)' in source


def test_worker_submission_callback_reestablishes_cadence_before_one_final_db_snapshot() -> None:
    source = inspect.getsource(execution_worker.Worker._run_admin_leverage_sync)
    callback_start = source.index('async def _authorize_submission()')
    callback_end = source.index("job.context = {**ctx, 'submission_status': 'SUBMITTING'}", callback_start)
    callback = source[callback_start:callback_end]

    fresh_spec_adapter = callback.index('fresh_spec_hl = HyperliquidAdapter(')
    master_read = callback.index('fresh_master_state, fresh_spec = await asyncio.gather(')
    master_config = callback.index('fresh_master_cfg = position_configs(fresh_master_state).get(job.asset)')
    effective_exchange_max = callback.index('effective_exchange_max = min(fresh_spec.max_leverage, initial_exchange_max)')
    cadence_refresh = callback.index('reestablish_submission_slot_before_final_authorization(')
    final_db_auth = callback.index('leverage, is_cross, risk_max_leverage = await fresh_position_config_sync_authorization(')
    submitted_update = callback.index("submitted['leverage'] = leverage")

    assert (
        fresh_spec_adapter
        < master_read
        < master_config
        < effective_exchange_max
        < cadence_refresh
        < final_db_auth
        < submitted_update
    )
    assert 'fresh_master_hl.user_state(' in callback
    assert 'priority=Priority.ORDER' in callback
    assert 'fresh_spec_hl.asset_spec(job.asset)' in callback
    assert 'master_leverage=fresh_master_cfg.leverage' in callback
    assert 'exchange_max_leverage=effective_exchange_max' in callback
    assert 'fresh_master_cfg.is_cross' in callback
    assert 'not fresh_spec.only_isolated' in callback
    assert 'and initial_desired_is_cross' in callback
    assert 'desired_is_cross=fresh_desired_is_cross' in callback


def test_degraded_slot_refresh_happens_only_when_sustained_mode_is_active() -> None:
    source = inspect.getsource(AddressActionTracker.reestablish_submission_slot_before_final_authorization)
    exists_index = source.index('await self._redis.exists(mode_key)')
    return_index = source.index('return', exists_index)
    set_index = source.index('await self._redis.set(', return_index)
    assert exists_index < return_index < set_index
    assert 'px=ADDRESS_BACKOFF_SECONDS * 1000' in source
    assert 'nx=True' not in source


def test_signing_material_is_metadata_prerequisite_not_policy_or_decrypt_gate() -> None:
    source = inspect.getsource(admin._position_config_sync_signing_material)
    assert 'select(TradingAccount)' in source
    assert 'select(SigningCredential)' in source
    assert '_credential_active(cred)' in source
    assert 'RiskProfile' not in source
    assert 'SystemFlag' not in source
    assert 'copy_state' not in source
    assert 'crypto.decrypt' not in source


def test_diagnostic_can_reuse_preloaded_follower_adapter() -> None:
    source = inspect.getsource(admin._position_config_diagnostic)
    assert 'follower_hl: HyperliquidAdapter | None = None' in source
    assert 'follower_hl = follower_hl or _follower_adapter(network)' in source
    assert 'follower_hl.network != network' in source


def test_success_audit_uses_worker_refreshed_source_values() -> None:
    worker_source = inspect.getsource(execution_worker.Worker._run_admin_leverage_sync)
    api_source = inspect.getsource(admin.sync_position_config)
    assert "refreshed_source['master'] = {" in worker_source
    assert "refreshed_source['exchange_max_leverage'] = fresh_spec.max_leverage" in worker_source
    assert "refreshed_source['exchange_only_isolated'] = fresh_spec.only_isolated" in worker_source
    assert "refreshed_source['effective_exchange_max_leverage'] = effective_exchange_max" in worker_source
    assert "refreshed_source['risk_max_leverage'] = risk_max_leverage" in worker_source
    assert "verified['master'] = dict(refreshed_source['master'])" in api_source
    assert "verified['exchange_max_leverage'] = refreshed_source['exchange_max_leverage']" in api_source
    assert "verified['exchange_only_isolated'] = refreshed_source['exchange_only_isolated']" in api_source
    assert "verified['effective_exchange_max_leverage'] = refreshed_source['effective_exchange_max_leverage']" in api_source
    assert "verified['risk_max_leverage'] = refreshed_source['risk_max_leverage']" in api_source


def test_cancelled_signed_sync_is_terminal_and_audited_in_worker() -> None:
    source = inspect.getsource(execution_worker.Worker._run_admin_leverage_sync)
    signed_update = source.index('response = await follower_hl.update_leverage')
    cancellation = source.index('except asyncio.CancelledError:', signed_update)
    audit_index = source.index("action='ADMIN_FOLLOWER_LEVERAGE_SYNC_UNVERIFIED'", cancellation)
    dead_index = source.index('JobState.DEAD', audit_index)
    raise_index = source.index('\n            raise\n', dead_index)

    assert signed_update < cancellation < audit_index < dead_index < raise_index
    cancellation_block = source[cancellation:raise_index]
    assert "'response': None" in cancellation_block
    assert "'observed': None" in cancellation_block
    assert "'submission_status': 'UNKNOWN_DUE_TO_CANCELLATION'" in cancellation_block
    assert 'RETRYING' not in cancellation_block


def test_verification_read_failure_is_audited_after_worker_submission() -> None:
    source = inspect.getsource(admin.sync_position_config)
    worker_wait = source.index('worker_result = await _wait_position_config_sync_job')
    verification_start = source.index(
        'follower_state = await follower_hl.user_state(account_address, priority=Priority.DIAGNOSTIC)',
        worker_wait,
    )
    verification_error = source.index("verification_error = f'{type(exc).__name__}: {exc}'", verification_start)
    audit_index = source.index("action='ADMIN_FOLLOWER_LEVERAGE_SYNC_UNVERIFIED'", verification_error)
    commit_index = source.index('await db.commit()', audit_index)
    raise_index = source.index("raise HTTPException(502, f'Leverage update sent, but verification read failed", commit_index)

    assert worker_wait < verification_start < verification_error < audit_index < commit_index < raise_index
    failure_block = source[verification_error:raise_index]
    assert "'response': response" in failure_block
    assert "'desired': desired" in failure_block
    assert "'observed': None" in failure_block
    assert "'verification_error': verification_error" in failure_block
