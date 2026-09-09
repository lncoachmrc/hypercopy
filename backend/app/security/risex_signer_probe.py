from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.core.config import Network


ProbeVerdict = Literal['PASS', 'FAIL', 'UNKNOWN']
PermissionEvidenceSource = Literal['onchain', 'api', 'none']


@dataclass(frozen=True, slots=True)
class CapabilityCheck:
    name: str
    verdict: ProbeVerdict
    detail: str


@dataclass(frozen=True, slots=True)
class RISExSignerCapabilityEvidence:
    """Public security evidence only; never contains a private key or signature."""

    network: Network
    account: str
    signer: str
    chain_id: int | None
    auth_contract: str | None
    router: str | None
    session_active: bool | None
    session_account: str | None
    session_expiration: int | None
    permission_evidence_source: PermissionEvidenceSource
    permissions: frozenset[str] | None
    perps_order_succeeded: bool | None
    fund_movement_rejected: bool | None
    withdrawal_rejected: bool | None
    post_revoke_order_rejected: bool | None
    operatorhub_bypass_disabled: bool | None


@dataclass(frozen=True, slots=True)
class RISExSignerCapabilityReport:
    verdict: ProbeVerdict
    security_gate_passed: bool
    checks: tuple[CapabilityCheck, ...]


def _is_address(value: str | None) -> bool:
    if value is None or len(value) != 42 or not value.startswith('0x'):
        return False
    try:
        int(value[2:], 16)
    except ValueError:
        return False
    return True


def _bool_check(name: str, value: bool | None, *, pass_detail: str, fail_detail: str) -> CapabilityCheck:
    if value is True:
        return CapabilityCheck(name, 'PASS', pass_detail)
    if value is False:
        return CapabilityCheck(name, 'FAIL', fail_detail)
    return CapabilityCheck(name, 'UNKNOWN', 'Required evidence has not been collected')


def _deployment_check(evidence: RISExSignerCapabilityEvidence) -> CapabilityCheck:
    if evidence.chain_id is None or evidence.chain_id <= 0:
        return CapabilityCheck('deployment_identity', 'UNKNOWN', 'Chain ID has not been verified')
    if not _is_address(evidence.auth_contract) or not _is_address(evidence.router):
        return CapabilityCheck(
            'deployment_identity',
            'UNKNOWN',
            'Authorization contract and router must be verified runtime addresses',
        )
    return CapabilityCheck(
        'deployment_identity',
        'PASS',
        'Chain ID, Authorization contract and router are explicitly identified',
    )


def _account_binding_check(evidence: RISExSignerCapabilityEvidence) -> CapabilityCheck:
    if evidence.session_account is None:
        return CapabilityCheck('account_binding', 'UNKNOWN', 'Session signer account binding is unavailable')
    if evidence.session_account.lower() != evidence.account.lower():
        return CapabilityCheck('account_binding', 'FAIL', 'Session signer is bound to a different account')
    return CapabilityCheck('account_binding', 'PASS', 'Session signer is bound to the expected account')


def _expiration_check(evidence: RISExSignerCapabilityEvidence, *, now: int) -> CapabilityCheck:
    if evidence.session_expiration is None:
        return CapabilityCheck('session_expiration', 'UNKNOWN', 'Session signer expiration is unavailable')
    if evidence.session_expiration <= now:
        return CapabilityCheck('session_expiration', 'FAIL', 'Session signer is expired')
    return CapabilityCheck('session_expiration', 'PASS', 'Session signer is not expired')


def _permission_check(evidence: RISExSignerCapabilityEvidence) -> CapabilityCheck:
    if evidence.permissions is None:
        return CapabilityCheck(
            'least_privilege_permissions',
            'UNKNOWN',
            'No explicit signer permission set is available',
        )

    permissions = {value.strip().upper().replace('-', '_').replace(' ', '_') for value in evidence.permissions}
    dangerous = {
        'ALL',
        'MOVE_FUNDS',
        'MOVE_FUND',
        'MOVEFUND',
        'TRANSFER',
        'TRANSFERS',
        'WITHDRAW',
        'WITHDRAWAL',
        'WITHDRAWALS',
        'SPOT',
    }
    present_dangerous = sorted(permissions & dangerous)
    if present_dangerous:
        return CapabilityCheck(
            'least_privilege_permissions',
            'FAIL',
            f"Signer exposes forbidden permissions: {', '.join(present_dangerous)}",
        )

    if evidence.permission_evidence_source != 'onchain':
        return CapabilityCheck(
            'least_privilege_permissions',
            'UNKNOWN',
            'Permission evidence is not independently verified on-chain',
        )

    if 'PERPS' not in permissions:
        return CapabilityCheck(
            'least_privilege_permissions',
            'FAIL',
            'On-chain permission evidence does not include PERPS',
        )

    unknown = sorted(permissions - {'PERPS'})
    if unknown:
        return CapabilityCheck(
            'least_privilege_permissions',
            'UNKNOWN',
            f"Unclassified on-chain permissions require review: {', '.join(unknown)}",
        )

    return CapabilityCheck(
        'least_privilege_permissions',
        'PASS',
        'On-chain permission evidence is restricted to PERPS',
    )


def evaluate_signer_capabilities(
    evidence: RISExSignerCapabilityEvidence,
    *,
    now: int,
) -> RISExSignerCapabilityReport:
    """Evaluate evidence only; this function never changes provider runtime state."""

    checks = (
        _deployment_check(evidence),
        _bool_check(
            'session_active',
            evidence.session_active,
            pass_detail='Session signer is active',
            fail_detail='Session signer is inactive or revoked',
        ),
        _account_binding_check(evidence),
        _expiration_check(evidence, now=now),
        _permission_check(evidence),
        _bool_check(
            'perps_positive_test',
            evidence.perps_order_succeeded,
            pass_detail='Controlled perpetual order test succeeded',
            fail_detail='Controlled perpetual order test failed',
        ),
        _bool_check(
            'fund_movement_negative_test',
            evidence.fund_movement_rejected,
            pass_detail='Fund movement attempt was rejected by RISEx authorization',
            fail_detail='Fund movement was not rejected by RISEx authorization',
        ),
        _bool_check(
            'withdrawal_negative_test',
            evidence.withdrawal_rejected,
            pass_detail='Withdrawal attempt was rejected by RISEx authorization',
            fail_detail='Withdrawal was not rejected by RISEx authorization',
        ),
        _bool_check(
            'post_revoke_negative_test',
            evidence.post_revoke_order_rejected,
            pass_detail='Order after signer revocation was rejected',
            fail_detail='Order after signer revocation was not rejected',
        ),
        _bool_check(
            'operatorhub_bypass_disabled',
            evidence.operatorhub_bypass_disabled,
            pass_detail='TRAXION trading worker cannot use JWT/OperatorHub bypass',
            fail_detail='JWT/OperatorHub bypass remains reachable from the trading worker',
        ),
    )

    if any(check.verdict == 'FAIL' for check in checks):
        verdict: ProbeVerdict = 'FAIL'
    elif all(check.verdict == 'PASS' for check in checks):
        verdict = 'PASS'
    else:
        verdict = 'UNKNOWN'

    return RISExSignerCapabilityReport(
        verdict=verdict,
        security_gate_passed=verdict == 'PASS',
        checks=checks,
    )
