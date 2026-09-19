from __future__ import annotations

import asyncio
import copy
from dataclasses import asdict, replace
import importlib.util
import json
from pathlib import Path
import pickle
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.adapters.risex import RISExAdapter
from app.security.risex_order_codec import RISExPlaceOrder, build_place_order_action_hash
from app.security.risex_place_order_permit import RISExPreparedPlaceOrderPermit
from app.security.risex_place_order_request import prepare_place_order_request
from app.security.risex_pre_order_gate import authorize_pre_order_probe
from tests.unit.risex_replay_test_support import make_test_replay_architecture_attestation
from app.security.risex_signed_testnet_policy import SignedTestnetBlocked, SignedTestnetPolicy
from app.security.risex_signer_probe import RISExSignerCapabilityEvidence


ACCOUNT = '0x' + ('11' * 20)
SIGNER = '0x' + ('22' * 20)
OTHER_ACCOUNT = '0x' + ('33' * 20)
OTHER_SIGNER = '0x' + ('44' * 20)
AUTH = '0x' + ('aa' * 20)
ROUTER = '0x' + ('bb' * 20)
FIXED_NOW = 1_900_000_000.0
BLOCK_TIMESTAMP = 1_800_000_000
SESSION_EXPIRATION = 2_000_000_000


class MutableClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


async def _run_readiness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    session_active: bool = True,
    perps_permission: bool = True,
    fund_movement_path_absent: bool = True,
    clock: MutableClock | None = None,
) -> Any:
    from app.security import risex_signed_testnet_runner as runner

    async def collect_deployment(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            block_number=0x1234,
            domain_verifying_contract=AUTH,
            system_router=ROUTER,
        )

    async def collect_authorization(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            block_timestamp=BLOCK_TIMESTAMP,
            session_expiration=SESSION_EXPIRATION,
            session_permission_bitmap=0xFFFFFFFF,
            stored_status_code=1,
            session_not_expired=True,
            session_active=session_active,
            all_permission_id=1,
            all_permission=True,
            perps_permission_id=2,
            perps_permission=perps_permission,
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

    runtime_clock = clock or MutableClock(FIXED_NOW)
    return await runner.run_signed_testnet_readiness(
        env={},
        api=SimpleNamespace(),
        rpc=SimpleNamespace(),
        network='testnet',
        explicit_approval=True,
        disposable_account_asserted=True,
        dedicated_signer_asserted=True,
        operatorhub_bypass_disabled=True,
        fund_movement_path_absent=fund_movement_path_absent,
        expected_fingerprint='test-fingerprint',
        clock=runtime_clock,
    )


def _assert_valid(attestation: object, *, clock: MutableClock) -> None:
    from app.security import risex_signed_testnet_runner as runner

    runner.assert_runtime_readiness_attested(
        attestation,
        account_address=ACCOUNT,
        signer_address=SIGNER,
        clock=clock,
    )


def _pre_order_gate(*, now: int) -> object:
    evidence = RISExSignerCapabilityEvidence(
        network='testnet',
        account=ACCOUNT,
        signer=SIGNER,
        chain_id=11155931,
        auth_contract=AUTH,
        router=ROUTER,
        session_active=True,
        session_account=ACCOUNT,
        session_expiration=now + 3600,
        onchain_perps_only_scope=False,
        perps_order_succeeded=None,
        fund_movement_rejected=True,
        withdrawal_rejected=True,
        post_revoke_order_rejected=None,
        operatorhub_bypass_disabled=True,
        perps_permission=True,
        fund_movement_path_absent=True,
    )
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
        evidence=evidence,
        now=now,
        replay_protection_architecture_attestation=(
            make_test_replay_architecture_attestation(
                request=_prepared_request(),
                chain_id=11155931,
                authorization_address=AUTH,
                router_address=ROUTER,
            )
        ),
    )


def _prepared_request() -> object:
    order = RISExPlaceOrder(
        market_id=1,
        size_steps=100,
        price_ticks=50_000,
        side=0,
        post_only=False,
        reduce_only=False,
        stp_mode=0,
        order_type=1,
        time_in_force=0,
        client_order_id=7,
        ttl_units=0,
    )
    permit = RISExPreparedPlaceOrderPermit(
        account_address=ACCOUNT,
        signer_address=SIGNER,
        action_hash=build_place_order_action_hash(order),
        nonce_anchor=43,
        nonce_bitmap_index=0,
        deadline=2_000_000_100,
        _signature=bytes([9]) * 65,
    )
    return prepare_place_order_request(order=order, permit=permit)


@pytest.mark.asyncio
async def test_01_runner_emits_runtime_attestation_only_for_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.security import risex_signed_testnet_runner as runner

    assert getattr(runner, 'RISExSignedTestnetReadinessResult', None) is not None
    assert getattr(runner, 'RISExRuntimeReadinessAttestation', None) is not None

    passed = await _run_readiness(monkeypatch)
    assert passed.report.verdict == 'PASS'
    assert passed.attestation is not None
    assert passed.attestation.verdict == 'PASS'
    assert passed.attestation.fund_movement_path_absent is True
    assert passed.attestation.block_tag == '0x1234'
    assert passed.attestation.account_address == ACCOUNT
    assert passed.attestation.signer_address == SIGNER
    assert passed.attestation.adr_reference == 'ADR-0002'
    assert passed.attestation.issued_at == FIXED_NOW
    assert passed.report.post_allowed is False
    assert passed.report.full_security_gate_passed is False
    assert passed.report.writes_enabled is False

    unknown = await _run_readiness(monkeypatch, fund_movement_path_absent=False)
    assert unknown.report.verdict == 'UNKNOWN'
    assert unknown.attestation is None

    failed = await _run_readiness(monkeypatch, perps_permission=False)
    assert failed.report.verdict == 'FAIL'
    assert failed.attestation is None


@pytest.mark.asyncio
async def test_02_runtime_attestation_cannot_be_constructed_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.security import risex_signed_testnet_runner as runner

    result = await _run_readiness(monkeypatch)
    cls = type(result.attestation)
    with pytest.raises(TypeError):
        cls(
            verdict='PASS',
            fund_movement_path_absent=True,
            block_tag='0x1234',
            account_address=ACCOUNT,
            signer_address=SIGNER,
            adr_reference='ADR-0002',
            issued_at=FIXED_NOW,
            network='testnet',
        )


@pytest.mark.asyncio
async def test_03_dict_or_json_cannot_reconstruct_runtime_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _run_readiness(monkeypatch)
    cls = type(result.attestation)
    payload = {
        'verdict': 'PASS',
        'fund_movement_path_absent': True,
        'block_tag': '0x1234',
        'account_address': ACCOUNT,
        'signer_address': SIGNER,
        'adr_reference': 'ADR-0002',
        'issued_at': FIXED_NOW,
        'network': 'testnet',
    }
    decoded = json.loads(json.dumps(payload))
    with pytest.raises(TypeError):
        cls(**decoded)


@pytest.mark.asyncio
async def test_04_fake_seal_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.security import risex_signed_testnet_runner as runner

    result = await _run_readiness(monkeypatch)
    cls = type(result.attestation)
    forged = object.__new__(cls)
    for name, value in {
        'verdict': 'PASS',
        'fund_movement_path_absent': True,
        'block_tag': '0x1234',
        'account_address': ACCOUNT,
        'signer_address': SIGNER,
        'adr_reference': 'ADR-0002',
        'issued_at': FIXED_NOW,
        'network': 'testnet',
        '_attestation_seal': object(),
    }.items():
        object.__setattr__(forged, name, value)

    with pytest.raises(SignedTestnetBlocked, match='attest|seal|readiness'):
        runner.assert_runtime_readiness_attested(
            forged,
            account_address=ACCOUNT,
            signer_address=SIGNER,
            clock=MutableClock(FIXED_NOW),
        )


@pytest.mark.asyncio
async def test_05_runtime_attestation_cannot_be_cloned_or_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _run_readiness(monkeypatch)
    attestation = result.attestation
    assert attestation is not None

    with pytest.raises(TypeError):
        replace(attestation, issued_at=FIXED_NOW + 1)
    with pytest.raises(TypeError):
        copy.copy(attestation)


@pytest.mark.asyncio
async def test_06_runtime_attestation_is_bound_to_account_and_signer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.security import risex_signed_testnet_runner as runner

    result = await _run_readiness(monkeypatch)
    attestation = result.attestation
    assert attestation is not None
    clock = MutableClock(FIXED_NOW)

    with pytest.raises(SignedTestnetBlocked, match='account|identity'):
        runner.assert_runtime_readiness_attested(
            attestation,
            account_address=OTHER_ACCOUNT,
            signer_address=SIGNER,
            clock=clock,
        )
    with pytest.raises(SignedTestnetBlocked, match='signer|identity'):
        runner.assert_runtime_readiness_attested(
            attestation,
            account_address=ACCOUNT,
            signer_address=OTHER_SIGNER,
            clock=clock,
        )


@pytest.mark.asyncio
async def test_07_runtime_attestation_ttl_boundary_uses_injected_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.security import risex_signed_testnet_runner as runner

    result = await _run_readiness(monkeypatch, clock=MutableClock(FIXED_NOW))
    attestation = result.attestation
    assert attestation is not None
    assert runner.RUNTIME_READINESS_TTL_SECONDS == 300

    boundary_clock = MutableClock(FIXED_NOW + 300)
    _assert_valid(attestation, clock=boundary_clock)

    boundary_clock.value = FIXED_NOW + 300.001
    with pytest.raises(SignedTestnetBlocked, match='expired|stale|readiness'):
        _assert_valid(attestation, clock=boundary_clock)


@pytest.mark.asyncio
async def test_08_valid_runtime_attestation_never_bypasses_pre_post_freshness_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression invariant: a valid readiness attestation must never bypass the
    live freshness probe immediately before POST. This test is intentionally kept
    even though 2C does not wire place_ioc; it prevents a later change from treating
    readiness PASS as a substitute for current on-chain session/deployment evidence.
    """
    from app.adapters.risex_signed_testnet_http import RISExSignedTestnetHTTPTransport

    result = await _run_readiness(monkeypatch)
    attestation = result.attestation
    assert attestation is not None
    adapter = RISExAdapter(
        network='testnet',
        gate3_mode='short_lived_attestation',
        readiness_attestation=attestation,
    )
    assert adapter.readiness_attestation is attestation

    calls: list[httpx.Request] = []
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(200, json={'success': True})
        ),
        trust_env=False,
    )
    transport = RISExSignedTestnetHTTPTransport(
        gate=_pre_order_gate(now=int(FIXED_NOW)),
        client=client,
        freshness_probe=None,
    )

    with pytest.raises(SignedTestnetBlocked, match='fresh session|freshness|fresh'):
        await transport.post_place_order(_prepared_request())  # type: ignore[arg-type]
    await client.aclose()

    assert calls == []


@pytest.mark.asyncio
async def test_09_runtime_attestation_is_process_local_and_not_pickleable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _run_readiness(monkeypatch)
    attestation = result.attestation
    assert attestation is not None

    with pytest.raises(TypeError):
        pickle.dumps(attestation)


@pytest.mark.asyncio
async def test_10_cli_serializes_only_result_report_and_never_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _run_readiness(monkeypatch)
    assert result.attestation is not None

    script_path = Path(__file__).resolve().parents[3] / 'scripts' / 'risex_signed_testnet_readiness.py'
    spec = importlib.util.spec_from_file_location('risex_signed_testnet_readiness_cli', script_path)
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    class AsyncContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
            return None

    async def fake_runner(**_kwargs: object) -> object:
        return result

    monkeypatch.setattr(cli, 'RISExReadOnlyHTTPTransport', lambda **_kwargs: AsyncContext())
    monkeypatch.setattr(cli, 'RISExReadOnlyRPCTransport', lambda **_kwargs: AsyncContext())
    monkeypatch.setattr(cli, 'run_signed_testnet_readiness', fake_runner)

    args = SimpleNamespace(
        base_url='https://example.invalid',
        rpc_url='https://example.invalid',
        explicit_approval=True,
        disposable_account_asserted=True,
        dedicated_signer_asserted=True,
        operatorhub_bypass_disabled=True,
        fund_movement_path_absent=True,
    )
    payload, exit_code = await cli._run(args)

    assert payload == asdict(result.report)
    assert 'attestation' not in payload
    assert '_attestation_seal' not in payload
    assert exit_code == 0
