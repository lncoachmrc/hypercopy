from __future__ import annotations

from dataclasses import replace
import importlib
import os
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock
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
from app.security.risex_pre_order_gate import RISExPreOrderProbeGate, authorize_pre_order_probe
from tests.unit.risex_replay_test_support import make_test_replay_architecture_attestation
from app.security.risex_signed_testnet_policy import SignedTestnetBlocked, SignedTestnetPolicy
from app.security.risex_signed_testnet_runner import RISExSignedTestnetReadinessResult
from app.security.risex_signer_probe import RISExSignerCapabilityEvidence


ACCOUNT = '0x' + ('11' * 20)
SIGNER = '0x' + ('22' * 20)
OTHER_ACCOUNT = '0x' + ('33' * 20)
OTHER_SIGNER = '0x' + ('44' * 20)
AUTH = '0x' + ('aa' * 20)
ROUTER = '0x' + ('bb' * 20)
OTHER_AUTH = '0x' + ('cc' * 20)
OTHER_ROUTER = '0x' + ('dd' * 20)
CHAIN_ID = 11155931
SESSION_EXPIRATION = 4_000_000_000
FIXED_NOW = 1_900_000_000.0


class MutableClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _orchestration_module() -> ModuleType:
    # Lazy import keeps RED failures inside individual tests rather than collection.
    return importlib.import_module('app.services.risex_signed_execution')


async def _genuine_readiness_result(
    monkeypatch: pytest.MonkeyPatch,
    *,
    clock: MutableClock,
    account: str = ACCOUNT,
    signer: str = SIGNER,
) -> RISExSignedTestnetReadinessResult:
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
        lambda _env: SimpleNamespace(account_address=account, signer_address=signer),
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
        clock=clock,
    )
    assert result.report.verdict == 'PASS'
    assert result.attestation is not None
    return result


def _evidence(
    *,
    account: str = ACCOUNT,
    signer: str = SIGNER,
    **overrides: object,
) -> RISExSignerCapabilityEvidence:
    # Behavioral True values are unit-test fixtures only; they are not a runtime
    # evidence source and must never be synthesized by the 2E orchestrator.
    values: dict[str, object] = {
        'network': 'testnet',
        'account': account,
        'signer': signer,
        'chain_id': CHAIN_ID,
        'auth_contract': AUTH,
        'router': ROUTER,
        'session_active': True,
        'session_account': account,
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


def _gate(
    *,
    account: str = ACCOUNT,
    signer: str = SIGNER,
    authorization_address: str = AUTH,
    router_address: str = ROUTER,
) -> object:
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
        evidence=_evidence(
            account=account,
            signer=signer,
            auth_contract=authorization_address,
            router=router_address,
        ),
        now=int(FIXED_NOW),
        replay_protection_architecture_attestation=(
            make_test_replay_architecture_attestation(
                request=_request(account=account, signer=signer),
                chain_id=CHAIN_ID,
                authorization_address=authorization_address,
                router_address=router_address,
            )
        ),
    )


def _request(*, account: str = ACCOUNT, signer: str = SIGNER) -> RISExPreparedPlaceOrderRequest:
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
        signer_address=signer,
        action_hash=build_place_order_action_hash(order),
        nonce_anchor=43,
        nonce_bitmap_index=0,
        deadline=SESSION_EXPIRATION - 1,
        _signature=bytes([9]) * 65,
    )
    return prepare_place_order_request(order=order, permit=permit)


def _job() -> SimpleNamespace:
    return SimpleNamespace(
        user_id=uuid.uuid4(),
        execution_epoch_id=uuid.uuid4(),
        execution_provider='risex',
        execution_network='testnet',
    )


async def _allow_epoch(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.adapters import risex as risex_module

    async def matcher(_db: object, _job: object) -> bool:
        return True

    monkeypatch.setattr(risex_module, 'job_matches_active_destination', matcher)


async def _fresh_probe() -> RISExSignerCapabilityEvidence:
    return _evidence()


def _arm_kwargs(
    *,
    gate: object,
    clock: MutableClock,
    freshness_probe: object = _fresh_probe,
    transport_client: httpx.AsyncClient | None = None,
) -> dict[str, object]:
    return {
        'env': {},
        'api': SimpleNamespace(),
        'rpc': SimpleNamespace(),
        'pre_order_gate': gate,
        'freshness_probe': freshness_probe,
        'explicit_approval': True,
        'disposable_account_asserted': True,
        'dedicated_signer_asserted': True,
        'operatorhub_bypass_disabled': True,
        'fund_movement_path_absent': True,
        'readiness_clock': clock,
        'transport_client': transport_client,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'invalid_gate',
    [
        None,
        object(),
        RISExPreOrderProbeGate(account_address=ACCOUNT, signer_address=SIGNER),
    ],
)
async def test_arm_requires_attested_pre_order_gate_before_readiness(
    monkeypatch: pytest.MonkeyPatch,
    invalid_gate: object,
) -> None:
    module = _orchestration_module()
    readiness = AsyncMock(side_effect=AssertionError('readiness must not run'))
    monkeypatch.setattr(module, 'run_signed_testnet_readiness', readiness)

    with pytest.raises(SignedTestnetBlocked):
        await module.arm_risex_signed_testnet_execution(
            **_arm_kwargs(gate=invalid_gate, clock=MutableClock(FIXED_NOW))
        )

    assert readiness.await_count == 0


@pytest.mark.asyncio
async def test_readiness_fail_returns_no_session(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _orchestration_module()
    clock = MutableClock(FIXED_NOW)
    valid = await _genuine_readiness_result(monkeypatch, clock=clock)
    failed = RISExSignedTestnetReadinessResult(
        report=replace(valid.report, verdict='FAIL'),
        attestation=None,
    )
    readiness = AsyncMock(return_value=failed)
    monkeypatch.setattr(module, 'run_signed_testnet_readiness', readiness)

    with pytest.raises(SignedTestnetBlocked):
        await module.arm_risex_signed_testnet_execution(
            **_arm_kwargs(gate=_gate(), clock=clock)
        )

    assert readiness.await_count == 1


@pytest.mark.asyncio
async def test_readiness_pass_without_attestation_returns_no_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _orchestration_module()
    clock = MutableClock(FIXED_NOW)
    valid = await _genuine_readiness_result(monkeypatch, clock=clock)
    missing = RISExSignedTestnetReadinessResult(report=valid.report, attestation=None)
    readiness = AsyncMock(return_value=missing)
    monkeypatch.setattr(module, 'run_signed_testnet_readiness', readiness)

    with pytest.raises(SignedTestnetBlocked):
        await module.arm_risex_signed_testnet_execution(
            **_arm_kwargs(gate=_gate(), clock=clock)
        )

    assert readiness.await_count == 1


@pytest.mark.asyncio
async def test_arm_rejects_readiness_account_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _orchestration_module()
    clock = MutableClock(FIXED_NOW)
    valid = await _genuine_readiness_result(monkeypatch, clock=clock)
    readiness = AsyncMock(return_value=valid)
    monkeypatch.setattr(module, 'run_signed_testnet_readiness', readiness)

    with pytest.raises(SignedTestnetBlocked, match='account identity'):
        await module.arm_risex_signed_testnet_execution(
            **_arm_kwargs(gate=_gate(account=OTHER_ACCOUNT), clock=clock)
        )

    assert readiness.await_count == 1


@pytest.mark.asyncio
async def test_arm_rejects_readiness_signer_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _orchestration_module()
    clock = MutableClock(FIXED_NOW)
    valid = await _genuine_readiness_result(monkeypatch, clock=clock)
    readiness = AsyncMock(return_value=valid)
    monkeypatch.setattr(module, 'run_signed_testnet_readiness', readiness)

    with pytest.raises(SignedTestnetBlocked, match='signer identity'):
        await module.arm_risex_signed_testnet_execution(
            **_arm_kwargs(gate=_gate(signer=OTHER_SIGNER), clock=clock)
        )

    assert readiness.await_count == 1


@pytest.mark.asyncio
async def test_arm_rejects_readiness_authorization_address_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _orchestration_module()
    clock = MutableClock(FIXED_NOW)
    valid = await _genuine_readiness_result(monkeypatch, clock=clock)
    readiness = AsyncMock(return_value=valid)
    monkeypatch.setattr(module, 'run_signed_testnet_readiness', readiness)

    with pytest.raises(SignedTestnetBlocked, match='authorization'):
        await module.arm_risex_signed_testnet_execution(
            **_arm_kwargs(
                gate=_gate(authorization_address=OTHER_AUTH),
                clock=clock,
            )
        )

    assert readiness.await_count == 1


@pytest.mark.asyncio
async def test_arm_rejects_readiness_router_address_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _orchestration_module()
    clock = MutableClock(FIXED_NOW)
    valid = await _genuine_readiness_result(monkeypatch, clock=clock)
    readiness = AsyncMock(return_value=valid)
    monkeypatch.setattr(module, 'run_signed_testnet_readiness', readiness)

    with pytest.raises(SignedTestnetBlocked, match='router'):
        await module.arm_risex_signed_testnet_execution(
            **_arm_kwargs(
                gate=_gate(router_address=OTHER_ROUTER),
                clock=clock,
            )
        )

    assert readiness.await_count == 1


@pytest.mark.asyncio
async def test_arm_accepts_matching_readiness_deployment_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _orchestration_module()
    clock = MutableClock(FIXED_NOW)
    valid = await _genuine_readiness_result(monkeypatch, clock=clock)
    readiness = AsyncMock(return_value=valid)
    monkeypatch.setattr(module, 'run_signed_testnet_readiness', readiness)

    session = await module.arm_risex_signed_testnet_execution(
        **_arm_kwargs(
            gate=_gate(authorization_address=AUTH, router_address=ROUTER),
            clock=clock,
        )
    )
    try:
        assert readiness.await_count == 1
        assert session.adapter.readiness_attestation is valid.attestation
    finally:
        await session._transport.aclose()


@pytest.mark.asyncio
async def test_successful_arm_runs_readiness_once_and_returns_exact_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _orchestration_module()
    clock = MutableClock(FIXED_NOW)
    valid = await _genuine_readiness_result(monkeypatch, clock=clock)
    readiness = AsyncMock(return_value=valid)
    monkeypatch.setattr(module, 'run_signed_testnet_readiness', readiness)

    session = await module.arm_risex_signed_testnet_execution(
        **_arm_kwargs(gate=_gate(), clock=clock)
    )
    try:
        assert readiness.await_count == 1
        assert isinstance(session, module.RISExSignedExecutionSession)
        assert isinstance(session.adapter, RISExAdapter)
        assert isinstance(session.adapter.transport, RISExSignedTestnetHTTPTransport)
        assert session.adapter.readiness_attestation is valid.attestation
    finally:
        await session._transport.aclose()


@pytest.mark.asyncio
async def test_arm_forwards_every_readiness_input_without_hard_coding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _orchestration_module()
    clock = MutableClock(FIXED_NOW)
    valid = await _genuine_readiness_result(monkeypatch, clock=clock)
    failed = RISExSignedTestnetReadinessResult(
        report=replace(valid.report, verdict='FAIL'),
        attestation=None,
    )
    readiness = AsyncMock(return_value=failed)
    monkeypatch.setattr(module, 'run_signed_testnet_readiness', readiness)

    env = {'sentinel': 'credential-environment'}
    api = object()
    rpc = object()
    gate = _gate()
    kwargs = _arm_kwargs(gate=gate, clock=clock)
    kwargs.update(
        env=env,
        api=api,
        rpc=rpc,
        explicit_approval=False,
        disposable_account_asserted=False,
        dedicated_signer_asserted=False,
        operatorhub_bypass_disabled=False,
        fund_movement_path_absent=False,
    )

    with pytest.raises(SignedTestnetBlocked):
        await module.arm_risex_signed_testnet_execution(**kwargs)

    readiness.assert_awaited_once_with(
        env=env,
        api=api,
        rpc=rpc,
        network='testnet',
        explicit_approval=False,
        disposable_account_asserted=False,
        dedicated_signer_asserted=False,
        operatorhub_bypass_disabled=False,
        fund_movement_path_absent=False,
        clock=clock,
    )


@pytest.mark.asyncio
async def test_expired_session_never_refreshes_readiness_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _orchestration_module()
    monkeypatch.setenv('RISEX_SIGNED_WRITES_ENABLED', 'true')
    clock = MutableClock(FIXED_NOW)
    valid = await _genuine_readiness_result(monkeypatch, clock=clock)
    readiness = AsyncMock(return_value=valid)
    monkeypatch.setattr(module, 'run_signed_testnet_readiness', readiness)
    await _allow_epoch(monkeypatch)

    async with await module.arm_risex_signed_testnet_execution(
        **_arm_kwargs(gate=_gate(), clock=clock)
    ) as session:
        assert readiness.await_count == 1
        clock.value = FIXED_NOW + 301
        with pytest.raises(ProviderWriteDisabled, match='cause 3'):
            await session.adapter.place_ioc(db=object(), job=_job(), request=_request())
        assert readiness.await_count == 1


@pytest.mark.asyncio
async def test_armed_session_still_runs_pre_post_freshness_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _orchestration_module()
    monkeypatch.setenv('RISEX_SIGNED_WRITES_ENABLED', 'true')
    clock = MutableClock(FIXED_NOW)
    valid = await _genuine_readiness_result(monkeypatch, clock=clock)
    readiness = AsyncMock(return_value=valid)
    monkeypatch.setattr(module, 'run_signed_testnet_readiness', readiness)
    await _allow_epoch(monkeypatch)
    freshness_calls = 0
    http_calls: list[httpx.Request] = []

    async def stale_probe() -> RISExSignerCapabilityEvidence:
        nonlocal freshness_calls
        freshness_calls += 1
        return replace(_evidence(), session_active=False)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: http_calls.append(request)
            or httpx.Response(200, json={'success': True})
        ),
        trust_env=False,
    )
    try:
        session = await module.arm_risex_signed_testnet_execution(
            **_arm_kwargs(
                gate=_gate(),
                clock=clock,
                freshness_probe=stale_probe,
                transport_client=client,
            )
        )
        async with session:
            with pytest.raises(ProviderWriteDisabled, match='cause 4'):
                await session.adapter.place_ioc(db=object(), job=_job(), request=_request())
        assert client.is_closed is False
    finally:
        await client.aclose()

    assert readiness.await_count == 1
    assert freshness_calls == 1
    assert http_calls == []


@pytest.mark.asyncio
async def test_async_context_closes_owned_transport_on_normal_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _orchestration_module()
    clock = MutableClock(FIXED_NOW)
    valid = await _genuine_readiness_result(monkeypatch, clock=clock)
    monkeypatch.setattr(module, 'run_signed_testnet_readiness', AsyncMock(return_value=valid))

    session = await module.arm_risex_signed_testnet_execution(
        **_arm_kwargs(gate=_gate(), clock=clock)
    )
    transport = session._transport
    async with session:
        assert transport._client.is_closed is False
    assert transport._client.is_closed is True


@pytest.mark.asyncio
async def test_async_context_closes_owned_transport_when_body_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _orchestration_module()
    clock = MutableClock(FIXED_NOW)
    valid = await _genuine_readiness_result(monkeypatch, clock=clock)
    monkeypatch.setattr(module, 'run_signed_testnet_readiness', AsyncMock(return_value=valid))

    session = await module.arm_risex_signed_testnet_execution(
        **_arm_kwargs(gate=_gate(), clock=clock)
    )
    transport = session._transport
    with pytest.raises(RuntimeError, match='boom'):
        async with session:
            raise RuntimeError('boom')
    assert transport._client.is_closed is True


@pytest.mark.asyncio
async def test_flag_absent_still_blocks_otherwise_armed_session_at_cause_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _orchestration_module()
    monkeypatch.delenv('RISEX_SIGNED_WRITES_ENABLED', raising=False)
    clock = MutableClock(FIXED_NOW)
    valid = await _genuine_readiness_result(monkeypatch, clock=clock)
    readiness = AsyncMock(return_value=valid)
    monkeypatch.setattr(module, 'run_signed_testnet_readiness', readiness)

    async with await module.arm_risex_signed_testnet_execution(
        **_arm_kwargs(gate=_gate(), clock=clock)
    ) as session:
        with pytest.raises(ProviderWriteDisabled, match='cause 1'):
            await session.adapter.place_ioc(db=object(), job=_job(), request=_request())

    assert readiness.await_count == 1


@pytest.mark.asyncio
async def test_arm_never_mutates_signed_write_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _orchestration_module()
    monkeypatch.delenv('RISEX_SIGNED_WRITES_ENABLED', raising=False)
    assert 'RISEX_SIGNED_WRITES_ENABLED' not in os.environ
    clock = MutableClock(FIXED_NOW)
    valid = await _genuine_readiness_result(monkeypatch, clock=clock)
    monkeypatch.setattr(module, 'run_signed_testnet_readiness', AsyncMock(return_value=valid))

    session = await module.arm_risex_signed_testnet_execution(
        **_arm_kwargs(gate=_gate(), clock=clock)
    )
    try:
        assert 'RISEX_SIGNED_WRITES_ENABLED' not in os.environ
    finally:
        await session._transport.aclose()
