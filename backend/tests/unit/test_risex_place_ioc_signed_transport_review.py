from __future__ import annotations

from types import SimpleNamespace
import uuid

import pytest

from app.adapters.risex import RISExAdapter
from app.adapters.risex_types import ProviderWriteDisabled
from app.security.risex_order_codec import RISExPlaceOrder, build_place_order_action_hash
from app.security.risex_place_order_permit import RISExPreparedPlaceOrderPermit
from app.security.risex_place_order_request import prepare_place_order_request


ACCOUNT = '0x' + ('11' * 20)
OTHER_ACCOUNT = '0x' + ('44' * 20)
SIGNER = '0x' + ('22' * 20)
AUTH = '0x' + ('aa' * 20)
ROUTER = '0x' + ('bb' * 20)
SESSION_EXPIRATION = 4_000_000_000


async def _runtime_attestation(monkeypatch: pytest.MonkeyPatch) -> object:
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
            session_expiration=SESSION_EXPIRATION,
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

    result = await runner.run_signed_testnet_readiness(
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
    )
    assert result.attestation is not None
    return result.attestation


def _job(*, provider: str = 'risex') -> SimpleNamespace:
    return SimpleNamespace(
        user_id=uuid.uuid4(),
        execution_epoch_id=uuid.uuid4(),
        execution_provider=provider,
        execution_network='testnet',
    )


def _request(*, account: str = ACCOUNT):
    order = RISExPlaceOrder(
        market_id=1,
        size_steps=100,
        price_ticks=50_000,
        side=0,
        post_only=False,
        reduce_only=False,
        stp_mode=0,
        order_type=1,
        time_in_force=3,
        client_order_id=7,
        ttl_units=0,
    )
    permit = RISExPreparedPlaceOrderPermit(
        account_address=account,
        signer_address=SIGNER,
        action_hash=build_place_order_action_hash(order),
        nonce_anchor=43,
        nonce_bitmap_index=0,
        deadline=SESSION_EXPIRATION - 1,
        _signature=bytes([9]) * 65,
    )
    return prepare_place_order_request(order=order, permit=permit)


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid_value', ['false', '1', 'TRUE', ' true', 'true ', ''])
async def test_only_exact_lowercase_true_enables_signed_write_gate(
    monkeypatch: pytest.MonkeyPatch,
    invalid_value: str,
) -> None:
    monkeypatch.setenv('RISEX_SIGNED_WRITES_ENABLED', invalid_value)
    adapter = RISExAdapter(network='testnet')

    with pytest.raises(ProviderWriteDisabled, match='cause 1'):
        await adapter.place_ioc(db=object(), job=_job(), request=_request())


@pytest.mark.asyncio
async def test_active_hyperliquid_epoch_is_rejected_as_cause_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.adapters import risex as risex_module

    monkeypatch.setenv('RISEX_SIGNED_WRITES_ENABLED', 'true')

    async def matcher(_db: object, _job: object) -> bool:
        return True

    monkeypatch.setattr(risex_module, 'job_matches_active_destination', matcher)
    adapter = RISExAdapter(network='testnet')

    with pytest.raises(ProviderWriteDisabled, match='cause 2'):
        await adapter.place_ioc(db=object(), job=_job(provider='hyperliquid'), request=_request())


@pytest.mark.asyncio
async def test_runtime_readiness_account_must_match_specific_order_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.adapters import risex as risex_module

    monkeypatch.setenv('RISEX_SIGNED_WRITES_ENABLED', 'true')

    async def matcher(_db: object, _job: object) -> bool:
        return True

    monkeypatch.setattr(risex_module, 'job_matches_active_destination', matcher)
    attestation = await _runtime_attestation(monkeypatch)
    adapter = RISExAdapter(
        network='testnet',
        readiness_attestation=attestation,  # type: ignore[arg-type]
    )

    with pytest.raises(ProviderWriteDisabled, match='cause 3'):
        await adapter.place_ioc(
            db=object(),
            job=_job(),
            request=_request(account=OTHER_ACCOUNT),
        )
