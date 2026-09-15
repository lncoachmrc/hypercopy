from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.adapters.risex import RISExAdapter
from app.adapters.risex_types import ProviderWriteDisabled
from app.security.risex_signed_testnet_policy import SignedTestnetBlocked


ACCOUNT = '0x' + ('11' * 20)
SIGNER = '0x' + ('22' * 20)
OTHER_ACCOUNT = '0x' + ('33' * 20)
AUTH = '0x' + ('aa' * 20)
ROUTER = '0x' + ('bb' * 20)
FIXED_NOW = 1_900_000_000.0


async def _ready_result(monkeypatch: pytest.MonkeyPatch):
    from app.security import risex_signed_testnet_runner as runner

    async def collect_deployment(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            block_number=0x1234,
            domain_verifying_contract=AUTH,
            system_router=ROUTER,
        )

    async def collect_authorization(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            block_timestamp=1_800_000_000,
            session_expiration=2_000_000_000,
            session_permission_bitmap=0xFFFFFFFF,
            stored_status_code=1,
            session_not_expired=True,
            session_active=True,
            all_permission_id=1,
            all_permission=True,
            perps_permission_id=2,
            perps_permission=True,
            spot_permission_id=3,
            spot_permission=True,
            move_fund_permission_id=4,
            move_fund_permission=True,
            perps_only_scope=False,
        )

    monkeypatch.setattr(runner, 'reject_main_wallet_key_inputs', lambda _env: None)
    monkeypatch.setattr(runner, 'assert_signed_testnet_probe_allowed', lambda _policy: None)
    monkeypatch.setattr(runner, 'collect_runtime_deployment_evidence', collect_deployment)
    monkeypatch.setattr(
        runner,
        'evaluate_pinned_deployment_preflight',
        lambda _deployment, **_kwargs: SimpleNamespace(
            verdict='PASS',
            deployment_identity_verified=True,
        ),
    )
    monkeypatch.setattr(
        runner,
        'load_testnet_signer_credential',
        lambda _env: SimpleNamespace(account_address=ACCOUNT, signer_address=SIGNER),
    )
    monkeypatch.setattr(runner, 'collect_authorization_session_evidence', collect_authorization)

    return await runner.run_signed_testnet_readiness(
        env={},
        api=SimpleNamespace(),
        rpc=SimpleNamespace(),
        network='testnet',
        explicit_approval=True,
        disposable_account_asserted=True,
        dedicated_signer_asserted=True,
        operatorhub_bypass_disabled=True,
        fund_movement_path_absent=True,
        expected_fingerprint='test-fingerprint',
        clock=lambda: FIXED_NOW,
    )


@pytest.mark.asyncio
async def test_attested_adapter_still_rejects_place_ioc_before_transport_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _ready_result(monkeypatch)
    assert result.attestation is not None
    adapter = RISExAdapter(
        network='testnet',
        gate3_mode='short_lived_attestation',
        readiness_attestation=result.attestation,
    )

    with pytest.raises(ProviderWriteDisabled):
        await adapter.place_ioc(asset='BTC', is_buy=True, size='1', price='1')


@pytest.mark.asyncio
async def test_genuine_runtime_attestation_rejects_public_field_tampering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.security.risex_signed_testnet_runner import assert_runtime_readiness_attested

    result = await _ready_result(monkeypatch)
    attestation = result.attestation
    assert attestation is not None

    object.__setattr__(attestation, 'account_address', OTHER_ACCOUNT)

    with pytest.raises(SignedTestnetBlocked, match='tamper|attest|identity'):
        assert_runtime_readiness_attested(
            attestation,
            account_address=OTHER_ACCOUNT,
            signer_address=SIGNER,
            clock=lambda: FIXED_NOW,
        )
