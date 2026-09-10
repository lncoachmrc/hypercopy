from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from app.security.risex_deployment_probe import (
    DeploymentCheck,
    RISExDeploymentEvidence,
    Verdict,
    evaluate_runtime_deployment,
)


@dataclass(frozen=True, slots=True)
class RISExDeploymentPreflightReport:
    verdict: Verdict
    deployment_identity_verified: bool
    observed_fingerprint: str | None
    expected_fingerprint: str
    checks: tuple[DeploymentCheck, ...]
    writes_enabled: Literal[False] = False


def _valid_address(value: str | None) -> bool:
    if not isinstance(value, str) or len(value) != 42 or not value.startswith('0x'):
        return False
    try:
        number = int(value[2:], 16)
    except ValueError:
        return False
    return number != 0


def _valid_code_hash(value: str | None) -> bool:
    if not isinstance(value, str) or len(value) != 66 or not value.startswith('0x'):
        return False
    try:
        int(value[2:], 16)
    except ValueError:
        return False
    return True


def canonical_deployment_fingerprint(evidence: RISExDeploymentEvidence) -> str | None:
    """Hash stable runtime identity fields; block height and ABI status are excluded."""

    addresses = (
        evidence.domain_verifying_contract,
        evidence.auth.address,
        evidence.auth.implementation,
        evidence.system_router,
        evidence.router.address,
        evidence.router.implementation,
    )
    hashes = (
        evidence.auth.runtime_code_keccak256,
        evidence.auth.implementation_code_keccak256,
        evidence.router.runtime_code_keccak256,
        evidence.router.implementation_code_keccak256,
    )
    if (
        not evidence.network
        or evidence.api_chain_id is None
        or evidence.api_chain_id <= 0
        or not evidence.domain_name
        or not evidence.domain_version
        or not all(_valid_address(value) for value in addresses)
        or not all(_valid_code_hash(value) for value in hashes)
    ):
        return None

    payload = {
        'network': evidence.network,
        'chain_id': evidence.api_chain_id,
        'domain_name': evidence.domain_name,
        'domain_version': evidence.domain_version,
        'authorization_proxy': evidence.auth.address.lower(),  # type: ignore[union-attr]
        'authorization_proxy_code_hash': evidence.auth.runtime_code_keccak256.lower(),  # type: ignore[union-attr]
        'authorization_implementation': evidence.auth.implementation.lower(),  # type: ignore[union-attr]
        'authorization_implementation_code_hash': evidence.auth.implementation_code_keccak256.lower(),  # type: ignore[union-attr]
        'router_proxy': evidence.router.address.lower(),  # type: ignore[union-attr]
        'router_proxy_code_hash': evidence.router.runtime_code_keccak256.lower(),  # type: ignore[union-attr]
        'router_implementation': evidence.router.implementation.lower(),  # type: ignore[union-attr]
        'router_implementation_code_hash': evidence.router.implementation_code_keccak256.lower(),  # type: ignore[union-attr]
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(canonical).hexdigest()


def _fingerprint_check(observed: str | None, expected: str) -> DeploymentCheck:
    if len(expected) != 64:
        return DeploymentCheck(
            'deployment_fingerprint',
            'UNKNOWN',
            'Expected deployment fingerprint is not configured correctly',
        )
    try:
        int(expected, 16)
    except ValueError:
        return DeploymentCheck(
            'deployment_fingerprint',
            'UNKNOWN',
            'Expected deployment fingerprint is not configured correctly',
        )
    if observed is None:
        return DeploymentCheck(
            'deployment_fingerprint',
            'UNKNOWN',
            'Observed deployment fingerprint cannot be derived from incomplete evidence',
        )
    if observed != expected.lower():
        return DeploymentCheck(
            'deployment_fingerprint',
            'FAIL',
            'Observed RISEx runtime deployment differs from the reviewed pin',
        )
    return DeploymentCheck(
        'deployment_fingerprint',
        'PASS',
        'Observed RISEx runtime deployment matches the reviewed pin',
    )


def evaluate_pinned_deployment_preflight(
    evidence: RISExDeploymentEvidence,
    *,
    expected_fingerprint: str,
) -> RISExDeploymentPreflightReport:
    """Verify deployment identity against a reviewed pin while keeping writes disabled."""

    runtime_report = evaluate_runtime_deployment(evidence)
    identity_checks = tuple(check for check in runtime_report.checks if check.name != 'abi_identity')
    observed = canonical_deployment_fingerprint(evidence)
    fingerprint_check = _fingerprint_check(observed, expected_fingerprint)
    checks = (*identity_checks, fingerprint_check)

    if any(check.verdict == 'FAIL' for check in checks):
        verdict: Verdict = 'FAIL'
    elif any(check.verdict == 'UNKNOWN' for check in checks):
        verdict = 'UNKNOWN'
    else:
        verdict = 'PASS'

    return RISExDeploymentPreflightReport(
        verdict=verdict,
        deployment_identity_verified=verdict == 'PASS',
        observed_fingerprint=observed,
        expected_fingerprint=expected_fingerprint.lower(),
        checks=checks,
    )
