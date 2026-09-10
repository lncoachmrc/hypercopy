from __future__ import annotations

from app.security.risex_deployment_probe import (
    ContractDeploymentEvidence,
    RISExDeploymentEvidence,
    canonical_deployment_fingerprint,
    evaluate_pinned_deployment_preflight,
    evaluate_runtime_deployment,
)


AUTH_ADDRESS = '0x' + '11' * 20
ROUTER_ADDRESS = '0x' + '22' * 20
IMPLEMENTATION_ADDRESS = '0x' + '33' * 20
OTHER_ADDRESS = '0x' + '44' * 20
PINNED_SYNTHETIC_FINGERPRINT = 'f744f66c9fe0ae7551a0abda48b3b8c07750f396e2c4624564742ae53fdeb826'


def _contract(address: str, *, abi_verified: bool | None = True) -> ContractDeploymentEvidence:
    return ContractDeploymentEvidence(
        address=address,
        runtime_code_bytes=1074,
        runtime_code_keccak256='0x' + '55' * 32,
        implementation=IMPLEMENTATION_ADDRESS,
        implementation_code_bytes=15558,
        implementation_code_keccak256='0x' + '66' * 32,
        abi_verified=abi_verified,
        required_functions_present=True if abi_verified is True else None,
    )


def _evidence(**changes: object) -> RISExDeploymentEvidence:
    values: dict[str, object] = {
        'network': 'testnet',
        'api_chain_id': 11155931,
        'rpc_chain_id': 11155931,
        'block_number': 53932888,
        'domain_name': 'RISEx',
        'domain_version': '1',
        'domain_verifying_contract': AUTH_ADDRESS,
        'system_auth_contract': AUTH_ADDRESS,
        'system_router': ROUTER_ADDRESS,
        'auth': _contract(AUTH_ADDRESS),
        'router': _contract(ROUTER_ADDRESS),
    }
    values.update(changes)
    return RISExDeploymentEvidence(**values)  # type: ignore[arg-type]


def test_runtime_deployment_pass_requires_same_run_identity_code_and_verified_abi() -> None:
    report = evaluate_runtime_deployment(_evidence())

    assert report.verdict == 'PASS'
    assert report.deployment_verified is True


def test_runtime_deployment_fails_closed_on_api_rpc_chain_mismatch() -> None:
    report = evaluate_runtime_deployment(_evidence(rpc_chain_id=1))

    assert report.verdict == 'FAIL'
    assert report.deployment_verified is False
    assert any(check.name == 'chain_identity' and check.verdict == 'FAIL' for check in report.checks)


def test_runtime_deployment_fails_when_domain_and_system_auth_disagree() -> None:
    report = evaluate_runtime_deployment(
        _evidence(system_auth_contract=OTHER_ADDRESS)
    )

    assert report.verdict == 'FAIL'
    assert report.deployment_verified is False


def test_runtime_deployment_fails_when_proxy_or_implementation_has_no_code() -> None:
    bad_auth = ContractDeploymentEvidence(
        address=AUTH_ADDRESS,
        runtime_code_bytes=0,
        runtime_code_keccak256=None,
        implementation=None,
        implementation_code_bytes=None,
        implementation_code_keccak256=None,
        abi_verified=None,
        required_functions_present=None,
    )

    report = evaluate_runtime_deployment(_evidence(auth=bad_auth))

    assert report.verdict == 'FAIL'
    assert report.deployment_verified is False


def test_runtime_deployment_is_unknown_when_explorer_cannot_verify_abi() -> None:
    report = evaluate_runtime_deployment(
        _evidence(
            auth=_contract(AUTH_ADDRESS, abi_verified=None),
            router=_contract(ROUTER_ADDRESS, abi_verified=None),
        )
    )

    assert report.verdict == 'UNKNOWN'
    assert report.deployment_verified is False
    assert any(check.name == 'abi_identity' and check.verdict == 'UNKNOWN' for check in report.checks)


def test_runtime_deployment_fails_if_verified_abi_lacks_required_surface() -> None:
    bad_router = ContractDeploymentEvidence(
        address=ROUTER_ADDRESS,
        runtime_code_bytes=1074,
        runtime_code_keccak256='0x' + '55' * 32,
        implementation=IMPLEMENTATION_ADDRESS,
        implementation_code_bytes=20742,
        implementation_code_keccak256='0x' + '66' * 32,
        abi_verified=True,
        required_functions_present=False,
    )

    report = evaluate_runtime_deployment(_evidence(router=bad_router))

    assert report.verdict == 'FAIL'
    assert report.deployment_verified is False


def test_deployment_fingerprint_is_stable_across_blocks_and_excludes_abi_status() -> None:
    unknown_abi = _evidence(
        auth=_contract(AUTH_ADDRESS, abi_verified=None),
        router=_contract(ROUTER_ADDRESS, abi_verified=None),
    )
    later_block = _evidence(
        block_number=99999999,
        auth=_contract(AUTH_ADDRESS, abi_verified=None),
        router=_contract(ROUTER_ADDRESS, abi_verified=None),
    )

    assert canonical_deployment_fingerprint(unknown_abi) == PINNED_SYNTHETIC_FINGERPRINT
    assert canonical_deployment_fingerprint(later_block) == PINNED_SYNTHETIC_FINGERPRINT


def test_pinned_preflight_passes_identity_when_runtime_matches_even_if_full_abi_is_unknown() -> None:
    evidence = _evidence(
        auth=_contract(AUTH_ADDRESS, abi_verified=None),
        router=_contract(ROUTER_ADDRESS, abi_verified=None),
    )

    report = evaluate_pinned_deployment_preflight(
        evidence,
        expected_fingerprint=PINNED_SYNTHETIC_FINGERPRINT,
    )

    assert report.verdict == 'PASS'
    assert report.deployment_identity_verified is True
    assert report.observed_fingerprint == PINNED_SYNTHETIC_FINGERPRINT
    assert report.writes_enabled is False


def test_pinned_preflight_fails_on_runtime_code_drift() -> None:
    changed_router = ContractDeploymentEvidence(
        address=ROUTER_ADDRESS,
        runtime_code_bytes=1074,
        runtime_code_keccak256='0x' + '77' * 32,
        implementation=IMPLEMENTATION_ADDRESS,
        implementation_code_bytes=15558,
        implementation_code_keccak256='0x' + '66' * 32,
        abi_verified=None,
        required_functions_present=None,
    )

    report = evaluate_pinned_deployment_preflight(
        _evidence(
            auth=_contract(AUTH_ADDRESS, abi_verified=None),
            router=changed_router,
        ),
        expected_fingerprint=PINNED_SYNTHETIC_FINGERPRINT,
    )

    assert report.verdict == 'FAIL'
    assert report.deployment_identity_verified is False
    assert report.writes_enabled is False
    assert any(check.name == 'deployment_fingerprint' and check.verdict == 'FAIL' for check in report.checks)


def test_pinned_preflight_is_unknown_when_same_run_identity_evidence_is_incomplete() -> None:
    report = evaluate_pinned_deployment_preflight(
        _evidence(block_number=None),
        expected_fingerprint=PINNED_SYNTHETIC_FINGERPRINT,
    )

    assert report.verdict == 'UNKNOWN'
    assert report.deployment_identity_verified is False
    assert report.writes_enabled is False
