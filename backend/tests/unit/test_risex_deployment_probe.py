from __future__ import annotations

from app.security.risex_deployment_probe import (
    ContractDeploymentEvidence,
    RISExDeploymentEvidence,
    evaluate_runtime_deployment,
)


AUTH_ADDRESS = '0x' + '11' * 20
ROUTER_ADDRESS = '0x' + '22' * 20
IMPLEMENTATION_ADDRESS = '0x' + '33' * 20
OTHER_ADDRESS = '0x' + '44' * 20


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
