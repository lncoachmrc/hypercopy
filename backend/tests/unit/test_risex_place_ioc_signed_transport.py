from __future__ import annotations

from dataclasses import replace
from time import time
from types import SimpleNamespace
from typing import Any, ClassVar, Literal, get_type_hints
import uuid

import httpx
import pytest

from app.adapters.risex import RISExAdapter
from app.adapters.risex_signed_testnet_http import RISExSignedTestnetHTTPTransport
from app.adapters.risex_types import ProviderWriteDisabled
from app.security.risex_order_codec import RISExPlaceOrder, build_place_order_action_hash
from app.security.risex_place_order_permit import RISExPreparedPlaceOrderPermit
from app.security.risex_place_order_request import (
    RISExPreparedPlaceOrderRequest,
    prepare_place_order_request,
)
from app.security.risex_pre_order_gate import authorize_pre_order_probe
from app.security.risex_signed_testnet_policy import SignedTestnetPolicy
from app.security.risex_signed_testnet_runner import RISExSignedTestnetReadinessReport
from app.security.risex_signer_probe import RISExSignerCapabilityEvidence


ACCOUNT = '0x' + ('11' * 20)
SIGNER = '0x' + ('22' * 20)
OTHER_SIGNER = '0x' + ('33' * 20)
AUTH = '0x' + ('aa' * 20)
ROUTER = '0x' + ('bb' * 20)
CHAIN_ID = 11155931
SESSION_EXPIRATION = 4_000_000_000
FIXED_NOW = 1_900_000_000.0


class MutableClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


async def _runtime_attestation(
    monkeypatch: pytest.MonkeyPatch,
    *,
    clock: MutableClock | None = None,
) -> object:
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

    issued_clock = clock or MutableClock(time())
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
        clock=issued_clock,
    )
    assert result.attestation is not None
    return result.attestation


def _evidence(**overrides: object) -> RISExSignerCapabilityEvidence:
    values: dict[str, object] = {
        'network': 'testnet',
        'account': ACCOUNT,
        'signer': SIGNER,
        'chain_id': CHAIN_ID,
        'auth_contract': AUTH,
        'router': ROUTER,
        'session_active': True,
        'session_account': ACCOUNT,
        'session_expiration': SESSION_EXPIRATION,
        'onchain_perps_only_scope': False,
        'perps_order_succeeded': None,
        'fund_movement_rejected': True,
        'withdrawal_rejected': True,
        'post_revoke_order_rejected': None,
        'operatorhub_bypass_disabled': True,
        'perps_permission': True,
        'fund_movement_path_absent': True,
    }
    values.update(overrides)
    return RISExSignerCapabilityEvidence(**values)  # type: ignore[arg-type]


def _gate() -> object:
    policy = SignedTestnetPolicy(
        network='testnet',
        explicit_approval=True,
        deployment_verdict='PASS',
        deployment_identity_verified=True,
        disposable_account_asserted=True,
        dedicated_signer_asserted=True,
        operatorhub_bypass_disabled=True,
    )
    return authorize_pre_order_probe(
        policy=policy,
        evidence=_evidence(),
        now=int(time()),
        replay_protection_verified=True,
    )


def _request(*, signer: str = SIGNER) -> RISExPreparedPlaceOrderRequest:
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
        account_address=ACCOUNT,
        signer_address=signer,
        action_hash=build_place_order_action_hash(order),
        nonce_anchor=43,
        nonce_bitmap_index=0,
        deadline=SESSION_EXPIRATION - 1,
        _signature=bytes([9]) * 65,
    )
    return prepare_place_order_request(order=order, permit=permit)


def _job(*, network: str = 'testnet') -> SimpleNamespace:
    return SimpleNamespace(
        user_id=uuid.uuid4(),
        execution_epoch_id=uuid.uuid4(),
        execution_provider='risex',
        execution_network=network,
    )


async def _allow_epoch(monkeypatch: pytest.MonkeyPatch, *, matches: bool = True) -> list[object]:
    from app.adapters import risex as risex_module

    calls: list[object] = []

    async def matcher(db: object, job: object) -> bool:
        calls.append((db, job))
        return matches

    monkeypatch.setattr(risex_module, 'job_matches_active_destination', matcher, raising=False)
    return calls


def _signed_transport(
    *,
    freshness_probe: Any,
    http_calls: list[httpx.Request] | None = None,
) -> tuple[RISExSignedTestnetHTTPTransport, httpx.AsyncClient, list[httpx.Request]]:
    calls = http_calls if http_calls is not None else []
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(200, json={'success': True})
        ),
        trust_env=False,
    )
    transport = RISExSignedTestnetHTTPTransport(
        gate=_gate(),  # type: ignore[arg-type]
        client=client,
        freshness_probe=freshness_probe,
    )
    return transport, client, calls


@pytest.mark.asyncio
async def test_flag_absent_is_cause_1_and_fails_before_destination_or_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv('RISEX_SIGNED_WRITES_ENABLED', raising=False)
    epoch_calls = await _allow_epoch(monkeypatch)
    adapter = RISExAdapter(network='testnet')

    with pytest.raises(ProviderWriteDisabled, match='cause 1'):
        await adapter.place_ioc(db=object(), job=_job(), request=_request())

    assert epoch_calls == []


@pytest.mark.asyncio
async def test_mainnet_from_active_epoch_is_cause_2_even_when_adapter_network_is_testnet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('RISEX_SIGNED_WRITES_ENABLED', 'true')
    epoch_calls = await _allow_epoch(monkeypatch)
    adapter = RISExAdapter(network='testnet')
    job = _job(network='mainnet')

    with pytest.raises(ProviderWriteDisabled, match='cause 2'):
        await adapter.place_ioc(db=object(), job=job, request=_request())

    assert len(epoch_calls) == 1


@pytest.mark.asyncio
async def test_epoch_fence_mismatch_is_cause_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('RISEX_SIGNED_WRITES_ENABLED', 'true')
    epoch_calls = await _allow_epoch(monkeypatch, matches=False)
    adapter = RISExAdapter(network='mainnet')

    with pytest.raises(ProviderWriteDisabled, match='cause 2'):
        await adapter.place_ioc(db=object(), job=_job(network='testnet'), request=_request())

    assert len(epoch_calls) == 1


@pytest.mark.asyncio
async def test_missing_runtime_attestation_is_cause_3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('RISEX_SIGNED_WRITES_ENABLED', 'true')
    await _allow_epoch(monkeypatch)
    adapter = RISExAdapter(network='testnet', readiness_attestation=None)

    with pytest.raises(ProviderWriteDisabled, match='cause 3'):
        await adapter.place_ioc(db=object(), job=_job(), request=_request())


@pytest.mark.asyncio
async def test_expired_runtime_attestation_is_cause_3_and_is_not_regenerated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('RISEX_SIGNED_WRITES_ENABLED', 'true')
    await _allow_epoch(monkeypatch)
    issued_clock = MutableClock(FIXED_NOW)
    attestation = await _runtime_attestation(monkeypatch, clock=issued_clock)
    validation_clock = MutableClock(FIXED_NOW + 301)
    adapter = RISExAdapter(
        network='testnet',
        readiness_attestation=attestation,  # type: ignore[arg-type]
        readiness_clock=validation_clock,
    )

    with pytest.raises(ProviderWriteDisabled, match='cause 3'):
        await adapter.place_ioc(db=object(), job=_job(), request=_request())

    assert validation_clock.value == FIXED_NOW + 301


@pytest.mark.asyncio
async def test_runtime_attestation_signer_mismatch_is_cause_3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('RISEX_SIGNED_WRITES_ENABLED', 'true')
    await _allow_epoch(monkeypatch)
    attestation = await _runtime_attestation(monkeypatch)
    adapter = RISExAdapter(network='testnet', readiness_attestation=attestation)  # type: ignore[arg-type]

    with pytest.raises(ProviderWriteDisabled, match='cause 3'):
        await adapter.place_ioc(db=object(), job=_job(), request=_request(signer=OTHER_SIGNER))


@pytest.mark.asyncio
async def test_stale_pre_order_gate_is_cause_4_before_http_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('RISEX_SIGNED_WRITES_ENABLED', 'true')
    await _allow_epoch(monkeypatch)
    attestation = await _runtime_attestation(monkeypatch)
    freshness_calls = 0

    async def stale_probe() -> RISExSignerCapabilityEvidence:
        nonlocal freshness_calls
        freshness_calls += 1
        return replace(_evidence(), session_active=False)

    transport, client, http_calls = _signed_transport(freshness_probe=stale_probe)
    adapter = RISExAdapter(
        network='mainnet',
        transport=transport,  # type: ignore[arg-type]
        readiness_attestation=attestation,  # type: ignore[arg-type]
    )
    try:
        with pytest.raises(ProviderWriteDisabled, match='cause 4'):
            await adapter.place_ioc(db=object(), job=_job(), request=_request())
    finally:
        await client.aclose()

    assert freshness_calls == 1
    assert http_calls == []


@pytest.mark.asyncio
async def test_all_conditions_call_signed_post_once_with_coherent_typed_request(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv('RISEX_SIGNED_WRITES_ENABLED', 'true')
    epoch_calls = await _allow_epoch(monkeypatch)
    attestation = await _runtime_attestation(monkeypatch)
    freshness_calls = 0

    async def fresh_probe() -> RISExSignerCapabilityEvidence:
        nonlocal freshness_calls
        freshness_calls += 1
        return _evidence()

    transport, client, http_calls = _signed_transport(freshness_probe=fresh_probe)
    original_post = transport.post_place_order
    transport_calls: list[RISExPreparedPlaceOrderRequest] = []

    async def counted_post(request: RISExPreparedPlaceOrderRequest) -> dict[str, Any]:
        transport_calls.append(request)
        return await original_post(request)

    monkeypatch.setattr(transport, 'post_place_order', counted_post)
    adapter = RISExAdapter(
        network='mainnet',
        transport=transport,  # type: ignore[arg-type]
        readiness_attestation=attestation,  # type: ignore[arg-type]
    )
    request = _request()
    try:
        result = await adapter.place_ioc(db=object(), job=_job(), request=request)
    finally:
        await client.aclose()

    assert result == {'success': True}
    assert len(epoch_calls) == 1
    assert freshness_calls == 1
    assert transport_calls == [request]
    assert len(http_calls) == 1
    assert isinstance(transport_calls[0], RISExPreparedPlaceOrderRequest)
    assert transport_calls[0].permit.account_address == ACCOUNT
    assert transport_calls[0].permit.signer_address == SIGNER
    assert transport_calls[0].order.time_in_force == 3
    assert transport_calls[0].json_for_testnet_transport()['builder_id'] == 0
    assert 'RISEx place_ioc attempt' in caplog.text
    assert SIGNER.lower() not in caplog.text.lower()


@pytest.mark.asyncio
async def test_other_five_risex_write_methods_remain_provider_write_disabled() -> None:
    adapter = RISExAdapter(network='testnet')
    methods = (
        adapter.cancel_order,
        adapter.update_leverage,
        adapter.register_signer,
        adapter.revoke_signer,
        adapter.approve_builder_fee,
    )
    for method in methods:
        with pytest.raises(ProviderWriteDisabled):
            await method(anything='ignored')


@pytest.mark.asyncio
async def test_valid_readiness_never_bypasses_transport_freshness_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('RISEX_SIGNED_WRITES_ENABLED', 'true')
    await _allow_epoch(monkeypatch)
    attestation = await _runtime_attestation(monkeypatch)
    freshness_calls = 0

    async def stale_probe() -> RISExSignerCapabilityEvidence:
        nonlocal freshness_calls
        freshness_calls += 1
        return replace(_evidence(), session_expiration=SESSION_EXPIRATION - 1)

    transport, client, http_calls = _signed_transport(freshness_probe=stale_probe)
    adapter = RISExAdapter(
        network='testnet',
        transport=transport,  # type: ignore[arg-type]
        readiness_attestation=attestation,  # type: ignore[arg-type]
    )
    try:
        with pytest.raises(ProviderWriteDisabled, match='cause 4'):
            await adapter.place_ioc(db=object(), job=_job(), request=_request())
    finally:
        await client.aclose()

    assert freshness_calls == 1
    assert http_calls == []


def test_write_capability_literals_remain_false() -> None:
    adapter_hints = get_type_hints(RISExAdapter)
    report_hints = get_type_hints(RISExSignedTestnetReadinessReport)

    assert adapter_hints['writes_enabled'] == ClassVar[Literal[False]]
    assert report_hints['full_security_gate_passed'] == Literal[False]
    assert report_hints['writes_enabled'] == Literal[False]
