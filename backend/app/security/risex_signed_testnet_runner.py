from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
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
) -> RISExSignedTestnetReadinessReport:
    """Collect signed-testnet readiness evidence without exposing any provider write path.

    ``perps_only_scope`` remains diagnostic evidence. ADR-0002 readiness instead
    requires an active session, Perps permission and an explicit assertion that
    the reviewed session-key fund-movement path remains absent.

    The runner intentionally stops at evidence collection. It never constructs the
    signed POST transport and cannot place, cancel, register, revoke, transfer or
    withdraw. A future signed action must consume a separate reviewed gate.
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

    return RISExSignedTestnetReadinessReport(
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
