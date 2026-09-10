from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Verdict = Literal['PASS', 'FAIL', 'UNKNOWN']


@dataclass(frozen=True, slots=True)
class ContractDeploymentEvidence:
    address: str | None
    runtime_code_bytes: int | None
    runtime_code_keccak256: str | None
    implementation: str | None
    implementation_code_bytes: int | None
    implementation_code_keccak256: str | None
    abi_verified: bool | None
    required_functions_present: bool | None


@dataclass(frozen=True, slots=True)
class RISExDeploymentEvidence:
    network: str
    api_chain_id: int | None
    rpc_chain_id: int | None
    block_number: int | None
    domain_name: str | None
    domain_version: str | None
    domain_verifying_contract: str | None
    system_auth_contract: str | None
    system_router: str | None
    auth: ContractDeploymentEvidence
    router: ContractDeploymentEvidence


@dataclass(frozen=True, slots=True)
class DeploymentCheck:
    name: str
    verdict: Verdict
    detail: str


@dataclass(frozen=True, slots=True)
class RISExDeploymentReport:
    verdict: Verdict
    deployment_verified: bool
    checks: tuple[DeploymentCheck, ...]


def _valid_address(value: str | None) -> bool:
    if not isinstance(value, str) or len(value) != 42 or not value.startswith('0x'):
        return False
    try:
        int(value[2:], 16)
    except ValueError:
        return False
    return int(value[2:], 16) != 0


def _same_address(left: str | None, right: str | None) -> bool:
    return _valid_address(left) and _valid_address(right) and left.lower() == right.lower()  # type: ignore[union-attr]


def _chain_check(evidence: RISExDeploymentEvidence) -> DeploymentCheck:
    if evidence.api_chain_id is None or evidence.rpc_chain_id is None or evidence.block_number is None:
        return DeploymentCheck('chain_identity', 'UNKNOWN', 'API/RPC chain or block evidence is incomplete')
    if evidence.api_chain_id <= 0 or evidence.rpc_chain_id <= 0 or evidence.block_number <= 0:
        return DeploymentCheck('chain_identity', 'FAIL', 'API/RPC chain or block evidence is invalid')
    if evidence.api_chain_id != evidence.rpc_chain_id:
        return DeploymentCheck('chain_identity', 'FAIL', 'API chainId does not match RPC eth_chainId')
    return DeploymentCheck('chain_identity', 'PASS', 'API and RPC identify the same positive chain at a concrete block')


def _domain_check(evidence: RISExDeploymentEvidence) -> DeploymentCheck:
    if not evidence.domain_name or not evidence.domain_version:
        return DeploymentCheck('authorization_identity', 'UNKNOWN', 'EIP-712 domain name/version is incomplete')
    if not _same_address(evidence.domain_verifying_contract, evidence.system_auth_contract):
        return DeploymentCheck('authorization_identity', 'FAIL', 'EIP-712 verifyingContract differs from system auth contract')
    if not _same_address(evidence.domain_verifying_contract, evidence.auth.address):
        return DeploymentCheck('authorization_identity', 'FAIL', 'Authorization code evidence is for a different address')
    return DeploymentCheck('authorization_identity', 'PASS', 'EIP-712 domain, system config and code evidence identify the same Authorization proxy')


def _router_check(evidence: RISExDeploymentEvidence) -> DeploymentCheck:
    if not _valid_address(evidence.system_router):
        return DeploymentCheck('router_identity', 'UNKNOWN', 'Runtime system config does not provide a usable router address')
    if not _same_address(evidence.system_router, evidence.router.address):
        return DeploymentCheck('router_identity', 'FAIL', 'Router code evidence is for a different address')
    return DeploymentCheck('router_identity', 'PASS', 'Runtime system config and code evidence identify the same router proxy')


def _contract_code_check(name: str, contract: ContractDeploymentEvidence) -> DeploymentCheck:
    if not _valid_address(contract.address):
        return DeploymentCheck(f'{name}_code', 'FAIL', f'{name} address is invalid')
    if not contract.runtime_code_bytes or contract.runtime_code_bytes <= 0 or not contract.runtime_code_keccak256:
        return DeploymentCheck(f'{name}_code', 'FAIL', f'{name} proxy has no proven runtime bytecode')
    if not _valid_address(contract.implementation):
        return DeploymentCheck(f'{name}_code', 'FAIL', f'{name} EIP-1967 implementation is missing or invalid')
    if (
        not contract.implementation_code_bytes
        or contract.implementation_code_bytes <= 0
        or not contract.implementation_code_keccak256
    ):
        return DeploymentCheck(f'{name}_code', 'FAIL', f'{name} implementation has no proven runtime bytecode')
    return DeploymentCheck(f'{name}_code', 'PASS', f'{name} proxy and EIP-1967 implementation both have pinned runtime bytecode')


def _abi_check(evidence: RISExDeploymentEvidence) -> DeploymentCheck:
    contracts = (evidence.auth, evidence.router)
    if any(contract.abi_verified is False for contract in contracts):
        return DeploymentCheck('abi_identity', 'FAIL', 'An ABI was checked and does not match the runtime deployment')
    if any(contract.required_functions_present is False for contract in contracts):
        return DeploymentCheck('abi_identity', 'FAIL', 'Verified ABI is missing required Authorization/Router surface')
    if any(
        contract.abi_verified is not True or contract.required_functions_present is not True
        for contract in contracts
    ):
        return DeploymentCheck('abi_identity', 'UNKNOWN', 'Authorization/Router ABI is not independently verified for this runtime implementation')
    return DeploymentCheck('abi_identity', 'PASS', 'Authorization and Router ABI are verified and expose the required surface')


def evaluate_runtime_deployment(evidence: RISExDeploymentEvidence) -> RISExDeploymentReport:
    """Evaluate same-run RISEx runtime identity evidence without enabling writes.

    FAIL dominates UNKNOWN. A PASS is intentionally strict: the API domain and
    system config must agree with RPC chain/code evidence, both critical
    contracts must resolve to live EIP-1967 implementations, and their ABI must
    be independently tied to those runtime implementations.
    """
    checks = (
        _chain_check(evidence),
        _domain_check(evidence),
        _router_check(evidence),
        _contract_code_check('authorization', evidence.auth),
        _contract_code_check('router', evidence.router),
        _abi_check(evidence),
    )
    if any(check.verdict == 'FAIL' for check in checks):
        verdict: Verdict = 'FAIL'
    elif any(check.verdict == 'UNKNOWN' for check in checks):
        verdict = 'UNKNOWN'
    else:
        verdict = 'PASS'
    return RISExDeploymentReport(
        verdict=verdict,
        deployment_verified=verdict == 'PASS',
        checks=checks,
    )
