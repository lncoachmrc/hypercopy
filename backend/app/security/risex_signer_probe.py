from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from app.adapters.risex_types import ProviderReadUnavailable, RISExTransport
from app.core.config import Network


ProbeVerdict = Literal['PASS', 'FAIL', 'UNKNOWN']


@dataclass(frozen=True, slots=True)
class CapabilityCheck:
    name: str
    verdict: ProbeVerdict
    detail: str


@dataclass(frozen=True, slots=True)
class RISExSignerCapabilityEvidence:
    """Public security evidence only; never contains a private key or signature.

    ``onchain_perps_only_scope`` is deliberately semantic rather than a provider
    permission label. ``True`` means independent inspection of the exact deployed
    authorization path proves this registered signer can authorize only perpetual
    execution actions. ``False`` means the signer can authorize a broader class of
    actions. ``None`` means that proof has not been established.
    """

    network: Network
    account: str
    signer: str
    chain_id: int | None
    auth_contract: str | None
    router: str | None
    session_active: bool | None
    session_account: str | None
    session_expiration: int | None
    onchain_perps_only_scope: bool | None
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


def _unwrap_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get('data')
    if isinstance(data, dict):
        return data
    return payload


async def _read_json(
    transport: RISExTransport,
    path: str,
    *,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        payload = await transport.get_json(path, params=params)
    except Exception as exc:
        raise ProviderReadUnavailable(f'RISEx read failed at {path}: {exc}') from exc
    if not isinstance(payload, dict):
        raise ProviderReadUnavailable(f'RISEx read returned a non-object payload at {path}')
    return _unwrap_data(payload)


def _optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


async def collect_public_signer_evidence(
    transport: RISExTransport,
    *,
    network: Network,
    account: str,
    signer: str,
) -> RISExSignerCapabilityEvidence:
    """Collect public/read-only RISEx evidence without signing or mutating anything.

    Provider-supplied ``permission``/``permissions`` labels are intentionally not
    consumed. The current official registration contract described by RISEx does
    not expose a granular permission field, so least-privilege scope must be
    established independently from the deployed authorization path.
    """

    domain = await _read_json(transport, '/v1/auth/eip712-domain')
    system = await _read_json(transport, '/v1/system/config')
    status = await _read_json(
        transport,
        '/v1/auth/session-key-status',
        params={'account': account, 'signer': signer},
    )
    signers = await _read_json(
        transport,
        '/v1/auth/signers',
        params={'account': account},
    )

    chain_id = _optional_int(domain.get('chain_id', domain.get('chainId')))
    auth_contract = _optional_str(
        domain.get('verifying_contract', domain.get('verifyingContract'))
    )

    addresses = system.get('addresses')
    router = _optional_str(addresses.get('router')) if isinstance(addresses, dict) else None
    if router is None:
        router = _optional_str(system.get('router'))

    explicit_active = status.get('active')
    session_active = explicit_active if isinstance(explicit_active, bool) else None

    matching_signer: dict[str, Any] | None = None
    raw_signers = signers.get('signers')
    if isinstance(raw_signers, list):
        for row in raw_signers:
            if not isinstance(row, dict):
                continue
            row_signer = row.get('signer')
            if isinstance(row_signer, str) and row_signer.lower() == signer.lower():
                matching_signer = row
                break

    if matching_signer is None:
        session_account = None
        session_expiration = None
    else:
        session_account = _optional_str(matching_signer.get('account'))
        session_expiration = _optional_int(matching_signer.get('expiration'))

    return RISExSignerCapabilityEvidence(
        network=network,
        account=account,
        signer=signer,
        chain_id=chain_id,
        auth_contract=auth_contract,
        router=router,
        session_active=session_active,
        session_account=session_account,
        session_expiration=session_expiration,
        onchain_perps_only_scope=None,
        perps_order_succeeded=None,
        fund_movement_rejected=None,
        withdrawal_rejected=None,
        post_revoke_order_rejected=None,
        operatorhub_bypass_disabled=None,
    )


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


def _authorization_scope_check(evidence: RISExSignerCapabilityEvidence) -> CapabilityCheck:
    if evidence.onchain_perps_only_scope is None:
        return CapabilityCheck(
            'onchain_authorization_scope',
            'UNKNOWN',
            'Exact deployed authorization path has not been proven perps-only on-chain',
        )
    if evidence.onchain_perps_only_scope is False:
        return CapabilityCheck(
            'onchain_authorization_scope',
            'FAIL',
            'Registered signer authorization scope is broader than perpetual execution',
        )
    return CapabilityCheck(
        'onchain_authorization_scope',
        'PASS',
        'Exact deployed authorization path proves the registered signer is perps-only',
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
        _authorization_scope_check(evidence),
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
