from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from math import isfinite
from time import time as _system_clock
from typing import Literal

from app.core.config import Network
from app.security.risex_authorization_session import (
    RISExAuthorizationSessionEvidence,
    collect_authorization_session_evidence,
)
from app.security.risex_deployment_preflight import evaluate_pinned_deployment_preflight
from app.security.risex_deployment_probe import Verdict
from app.security.risex_deployment_runtime import (
    PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT,
    PublicAPITransport,
    PublicRPCTransport,
    collect_runtime_deployment_evidence,
)
from app.security.risex_signed_testnet_policy import (
    SignedTestnetBlocked,
    SignedTestnetPolicy,
    assert_signed_testnet_probe_allowed,
    reject_main_wallet_key_inputs,
)
from app.security.risex_testnet_signer import load_testnet_signer_credential


ADR_REFERENCE: Literal['ADR-0002'] = 'ADR-0002'
RUNTIME_READINESS_TTL_SECONDS = 300
RuntimeClock = Callable[[], float]
_RUNTIME_READINESS_SEAL = object()


@dataclass(frozen=True, slots=True)
class RISExSignedTestnetReadinessReport:
    verdict: Verdict
    deployment_verdict: Verdict
    deployment_identity_verified: bool
    block_tag: str
    account_address: str
    signer_address: str
    authorization_address: str
    router_address: str
    observed_block_timestamp: int
    session_expiration: int
    session_permission_bitmap: int
    session_permission_bitmap_hex: str
    stored_session_status_code: int
    session_not_expired: bool
    session_active: bool | None
    all_permission_id: int
    all_permission: bool
    perps_permission_id: int
    perps_permission: bool
    spot_permission_id: int
    spot_permission: bool
    move_fund_permission_id: int
    move_fund_permission: bool
    perps_only_scope: bool | None
    fund_movement_path_absent: bool
    adr_reference: Literal['ADR-0002']
    post_allowed: Literal[False]
    full_security_gate_passed: Literal[False] = False
    writes_enabled: Literal[False] = False


@dataclass(frozen=True, slots=True, init=False)
class RISExRuntimeReadinessAttestation:
    """Process-local proof that this runner recently produced ADR-0002 readiness PASS.

    Instances are sealed and can only be issued by ``run_signed_testnet_readiness``.
    There is deliberately no public constructor, dict/JSON loader, persistence path,
    clone path or pickle path. The private issuance snapshot binds the seal to the
    exact public values emitted by the runner, so mutation of a genuine instance is
    detected during validation.

    The attestation is a recent-context capability only; it never replaces the
    mandatory live on-chain freshness probe immediately before the provider POST.
    The mandatory regression test
    ``test_08_valid_runtime_attestation_never_bypasses_pre_post_freshness_probe``
    exists specifically to prevent a future change from skipping that pre-POST
    freshness probe merely because this attestation is valid.
    """

    verdict: Literal['PASS']
    fund_movement_path_absent: Literal[True]
    block_tag: str
    account_address: str
    signer_address: str
    adr_reference: Literal['ADR-0002']
    issued_at: float
    network: Literal['testnet']
    _attestation_seal: object = field(repr=False, compare=False)
    _attested_values: tuple[object, ...] = field(repr=False, compare=False)

    def __copy__(self) -> None:
        raise TypeError('RISEx runtime readiness attestation cannot be copied')

    def __deepcopy__(self, _memo: object) -> None:
        raise TypeError('RISEx runtime readiness attestation cannot be copied')

    def __reduce_ex__(self, _protocol: int) -> None:
        raise TypeError('RISEx runtime readiness attestation cannot be serialized')


@dataclass(frozen=True, slots=True)
class RISExSignedTestnetReadinessResult:
    """Readiness report plus an optional process-local runtime capability."""

    report: RISExSignedTestnetReadinessReport
    attestation: RISExRuntimeReadinessAttestation | None


def _is_address(value: str) -> bool:
    if len(value) != 42 or not value.startswith('0x'):
        return False
    try:
        int(value[2:], 16)
    except ValueError:
        return False
    return True


def _clock_value(clock: RuntimeClock) -> float:
    try:
        value = float(clock())
    except (TypeError, ValueError, OverflowError) as exc:
        raise SignedTestnetBlocked('RISEx runtime readiness clock is invalid') from exc
    if not isfinite(value):
        raise SignedTestnetBlocked('RISEx runtime readiness clock is invalid')
    return value


def _runtime_attested_values(
    *,
    verdict: str,
    fund_movement_path_absent: bool,
    block_tag: str,
    account_address: str,
    signer_address: str,
    adr_reference: str,
    issued_at: float,
    network: str,
) -> tuple[object, ...]:
    return (
        verdict,
        fund_movement_path_absent,
        block_tag,
        account_address,
        signer_address,
        adr_reference,
        issued_at,
        network,
    )


def assert_runtime_readiness_attested(
    attestation: object,
    *,
    account_address: str,
    signer_address: str,
    clock: RuntimeClock,
) -> None:
    """Fail closed unless a sealed, identity-bound, unexpired readiness PASS is present.

    The injected clock makes the 300-second lifetime deterministic in tests and keeps
    wall-clock access outside the attestation itself. This check limits reuse of an
    old readiness decision across runtime contexts; revocation/session freshness is
    still handled by the separate live pre-POST on-chain revalidation.
    """

    if not isinstance(attestation, RISExRuntimeReadinessAttestation):
        raise SignedTestnetBlocked('RISEx runtime readiness requires an attested PASS')
    if getattr(attestation, '_attestation_seal', None) is not _RUNTIME_READINESS_SEAL:
        raise SignedTestnetBlocked('RISEx runtime readiness attestation seal is invalid')

    current_values = _runtime_attested_values(
        verdict=attestation.verdict,
        fund_movement_path_absent=attestation.fund_movement_path_absent,
        block_tag=attestation.block_tag,
        account_address=attestation.account_address,
        signer_address=attestation.signer_address,
        adr_reference=attestation.adr_reference,
        issued_at=attestation.issued_at,
        network=attestation.network,
    )
    if getattr(attestation, '_attested_values', None) != current_values:
        raise SignedTestnetBlocked('RISEx runtime readiness attestation was tampered with')

    if (
        attestation.verdict != 'PASS'
        or attestation.fund_movement_path_absent is not True
        or attestation.adr_reference != ADR_REFERENCE
        or attestation.network != 'testnet'
    ):
        raise SignedTestnetBlocked('RISEx runtime readiness attestation is invalid')
    if not _is_address(attestation.account_address) or not _is_address(attestation.signer_address):
        raise SignedTestnetBlocked('RISEx runtime readiness attestation identity is invalid')
    if not _is_address(account_address) or attestation.account_address.lower() != account_address.lower():
        raise SignedTestnetBlocked('RISEx runtime readiness account identity does not match')
    if not _is_address(signer_address) or attestation.signer_address.lower() != signer_address.lower():
        raise SignedTestnetBlocked('RISEx runtime readiness signer identity does not match')
    if not isinstance(attestation.block_tag, str) or not attestation.block_tag.startswith('0x'):
        raise SignedTestnetBlocked('RISEx runtime readiness block tag is invalid')
    try:
        if int(attestation.block_tag, 16) < 0:
            raise ValueError
    except ValueError as exc:
        raise SignedTestnetBlocked('RISEx runtime readiness block tag is invalid') from exc
    if not isfinite(attestation.issued_at):
        raise SignedTestnetBlocked('RISEx runtime readiness issuance timestamp is invalid')

    now = _clock_value(clock)
    age = now - attestation.issued_at
    if age < 0:
        raise SignedTestnetBlocked('RISEx runtime readiness attestation is from the future')
    if age > RUNTIME_READINESS_TTL_SECONDS:
        raise SignedTestnetBlocked('RISEx runtime readiness attestation expired or is stale')


def _authorization_verdict(
    evidence: RISExAuthorizationSessionEvidence,
    *,
    fund_movement_path_absent: bool,
) -> Verdict:
    if evidence.session_active is False or evidence.perps_permission is False:
        return 'FAIL'
    if (
        evidence.session_active is True
        and evidence.perps_permission is True
        and fund_movement_path_absent is True
    ):
        return 'PASS'
    return 'UNKNOWN'


async def run_signed_testnet_readiness(
    *,
    env: Mapping[str, str],
    api: PublicAPITransport,
    rpc: PublicRPCTransport,
    network: Network,
    explicit_approval: bool,
    disposable_account_asserted: bool,
    dedicated_signer_asserted: bool,
    operatorhub_bypass_disabled: bool,
    fund_movement_path_absent: bool = False,
    expected_fingerprint: str = PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT,
    clock: RuntimeClock = _system_clock,
) -> RISExSignedTestnetReadinessResult:
    """Collect readiness and, only on PASS, issue a sealed process-local capability.

    ``perps_only_scope`` remains diagnostic evidence. ADR-0002 readiness instead
    requires an active session, Perps permission and an explicit assertion that
    the reviewed session-key fund-movement path remains absent.

    The runner never constructs the signed POST transport and cannot place, cancel,
    register, revoke, transfer or withdraw. Its runtime attestation is deliberately
    short-lived and does not replace live pre-POST on-chain freshness validation.
    """

    reject_main_wallet_key_inputs(env)
    if network != 'testnet':
        raise SignedTestnetBlocked('RISEx signed verification is testnet-only')

    deployment = await collect_runtime_deployment_evidence(
        api,
        rpc,
        network=network,
    )
    deployment_report = evaluate_pinned_deployment_preflight(
        deployment,
        expected_fingerprint=expected_fingerprint,
    )

    policy = SignedTestnetPolicy(
        network=network,
        explicit_approval=explicit_approval,
        deployment_verdict=deployment_report.verdict,
        deployment_identity_verified=deployment_report.deployment_identity_verified,
        disposable_account_asserted=disposable_account_asserted,
        dedicated_signer_asserted=dedicated_signer_asserted,
        operatorhub_bypass_disabled=operatorhub_bypass_disabled,
    )
    assert_signed_testnet_probe_allowed(policy)

    if (
        deployment.block_number is None
        or deployment.domain_verifying_contract is None
        or deployment.system_router is None
    ):
        raise SignedTestnetBlocked(
            'Pinned RISEx deployment PASS lacks required runtime identity evidence'
        )

    credential = load_testnet_signer_credential(env)
    block_tag = hex(deployment.block_number)
    authorization = await collect_authorization_session_evidence(
        rpc,
        authorization_address=deployment.domain_verifying_contract,
        account=credential.account_address,
        signer=credential.signer_address,
        block_tag=block_tag,
    )
    verdict = _authorization_verdict(
        authorization,
        fund_movement_path_absent=fund_movement_path_absent,
    )

    report = RISExSignedTestnetReadinessReport(
        verdict=verdict,
        deployment_verdict=deployment_report.verdict,
        deployment_identity_verified=deployment_report.deployment_identity_verified,
        block_tag=block_tag,
        account_address=credential.account_address,
        signer_address=credential.signer_address,
        authorization_address=deployment.domain_verifying_contract,
        router_address=deployment.system_router,
        observed_block_timestamp=authorization.block_timestamp,
        session_expiration=authorization.session_expiration,
        session_permission_bitmap=authorization.session_permission_bitmap,
        session_permission_bitmap_hex=f'0x{authorization.session_permission_bitmap:08X}',
        stored_session_status_code=authorization.stored_status_code,
        session_not_expired=authorization.session_not_expired,
        session_active=authorization.session_active,
        all_permission_id=authorization.all_permission_id,
        all_permission=authorization.all_permission,
        perps_permission_id=authorization.perps_permission_id,
        perps_permission=authorization.perps_permission,
        spot_permission_id=authorization.spot_permission_id,
        spot_permission=authorization.spot_permission,
        move_fund_permission_id=authorization.move_fund_permission_id,
        move_fund_permission=authorization.move_fund_permission,
        perps_only_scope=authorization.perps_only_scope,
        fund_movement_path_absent=fund_movement_path_absent,
        adr_reference=ADR_REFERENCE,
        post_allowed=False,
    )

    attestation: RISExRuntimeReadinessAttestation | None = None
    if report.verdict == 'PASS' and report.fund_movement_path_absent is True:
        issued_at = _clock_value(clock)
        attested_values = _runtime_attested_values(
            verdict='PASS',
            fund_movement_path_absent=True,
            block_tag=report.block_tag,
            account_address=report.account_address,
            signer_address=report.signer_address,
            adr_reference=ADR_REFERENCE,
            issued_at=issued_at,
            network='testnet',
        )
        attestation = object.__new__(RISExRuntimeReadinessAttestation)
        object.__setattr__(attestation, 'verdict', 'PASS')
        object.__setattr__(attestation, 'fund_movement_path_absent', True)
        object.__setattr__(attestation, 'block_tag', report.block_tag)
        object.__setattr__(attestation, 'account_address', report.account_address)
        object.__setattr__(attestation, 'signer_address', report.signer_address)
        object.__setattr__(attestation, 'adr_reference', ADR_REFERENCE)
        object.__setattr__(attestation, 'issued_at', issued_at)
        object.__setattr__(attestation, 'network', 'testnet')
        object.__setattr__(attestation, '_attestation_seal', _RUNTIME_READINESS_SEAL)
        object.__setattr__(attestation, '_attested_values', attested_values)
        assert_runtime_readiness_attested(
            attestation,
            account_address=report.account_address,
            signer_address=report.signer_address,
            clock=lambda: issued_at,
        )

    return RISExSignedTestnetReadinessResult(
        report=report,
        attestation=attestation,
    )
