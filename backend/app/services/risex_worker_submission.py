from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.hyperliquid import deterministic_cloid
from app.adapters.risex_http import RISExReadOnlyHTTPTransport
from app.adapters.risex_signed_testnet_http import RISExSignedTestnetHTTPTransport
from app.models.entities import (
    CopyJob,
    CopyState,
    Execution,
    ExecutionState,
    RiskHalt,
    RiskProfile,
    RiskState,
    SystemFlag,
    User,
    UserState,
)
from app.security.risex_authorization_session import collect_authorization_session_evidence
from app.security.risex_deployment_preflight import evaluate_pinned_deployment_preflight
from app.security.risex_deployment_runtime import (
    PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT,
    RISExReadOnlyRPCTransport,
    collect_runtime_deployment_evidence,
)
from app.security.risex_place_order_request import RISExPreparedPlaceOrderRequest
from app.security.risex_pre_order_gate import (
    RISExPreOrderProbeGate,
    assert_pre_order_probe_gate_attested,
    authorize_pre_order_probe,
)
from app.security.risex_replay_protection_architecture import (
    RISExReplayProtectionArchitectureAttestation,
    collect_replay_protection_architecture_attestation,
)
from app.security.risex_signed_testnet_policy import (
    SignedTestnetBlocked,
    SignedTestnetPolicy,
)
from app.security.risex_signer_probe import RISExSignerCapabilityEvidence
from app.security.risex_testnet_signer import load_testnet_signer_credential
from app.services.entitlement import entitlement
from app.services.risex_copy_execution import (
    RISExPreparedCopySubmission,
    client_order_id_for_job,
    persist_risex_pre_post_execution,
)
from app.services.risex_order_preparation import (
    assert_risex_plan_matches_intent,
    assert_risex_worker_write_allowed,
    make_freshness_probe,
    prepare_risex_ioc_plan,
    prepare_risex_ioc_request_from_plan,
)
from app.services.risex_risk_planning import (
    RISExRuntimeRiskFlags,
    parse_risex_portfolio_details,
    plan_risex_order_intent,
)


_RISEX_TESTNET_API_URL = 'https://api.testnet.rise.trade'
_RISEX_TESTNET_RPC_URL = 'https://testnet.riselabs.xyz'
_RISEX_USER_EXPOSURE_CEILING_USDC = Decimal('25000')
_RISEX_TOTAL_EXPOSURE_CEILING_USDC = Decimal('75000')


@dataclass(slots=True)
class RISExWorkerPreparedSubmission:
    """Own process-local transports plus the exact durable/signed submission."""

    transport: RISExSignedTestnetHTTPTransport
    submission: RISExPreparedCopySubmission
    api: RISExReadOnlyHTTPTransport
    rpc: RISExReadOnlyRPCTransport

    async def aclose(self) -> None:
        await self.transport.aclose()
        await self.api.aclose()
        await self.rpc.aclose()


async def build_risex_pre_order_gate_for_request(
    *,
    env: Mapping[str, str],
    api: RISExReadOnlyHTTPTransport,
    rpc: RISExReadOnlyRPCTransport,
    request: RISExPreparedPlaceOrderRequest,
    replay_protection_architecture_attestation: RISExReplayProtectionArchitectureAttestation,
    disposable_account_asserted: bool,
    dedicated_signer_asserted: bool,
    operatorhub_bypass_disabled: bool,
    fund_movement_path_absent: bool,
) -> RISExPreOrderProbeGate:
    """Build a sealed pre-order gate from live evidence for this signed request."""

    credential = load_testnet_signer_credential(env)
    deployment = await collect_runtime_deployment_evidence(
        api,
        rpc,
        network='testnet',
    )
    report = evaluate_pinned_deployment_preflight(
        deployment,
        expected_fingerprint=PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT,
    )
    if (
        deployment.block_number is None
        or deployment.api_chain_id is None
        or deployment.domain_verifying_contract is None
        or deployment.system_router is None
    ):
        raise SignedTestnetBlocked('RISEx runtime deployment evidence is incomplete')

    authorization = await collect_authorization_session_evidence(
        rpc,
        authorization_address=deployment.domain_verifying_contract,
        account=credential.account_address,
        signer=credential.signer_address,
        block_tag=hex(deployment.block_number),
    )
    policy = SignedTestnetPolicy(
        network='testnet',
        explicit_approval=True,
        deployment_verdict=report.verdict,
        deployment_identity_verified=report.deployment_identity_verified,
        disposable_account_asserted=disposable_account_asserted,
        dedicated_signer_asserted=dedicated_signer_asserted,
        operatorhub_bypass_disabled=operatorhub_bypass_disabled,
    )
    evidence = RISExSignerCapabilityEvidence(
        network='testnet',
        account=credential.account_address,
        signer=credential.signer_address,
        chain_id=deployment.api_chain_id,
        auth_contract=deployment.domain_verifying_contract,
        router=deployment.system_router,
        session_active=authorization.session_active,
        session_account=authorization.account,
        session_expiration=authorization.session_expiration,
        onchain_perps_only_scope=authorization.perps_only_scope,
        perps_order_succeeded=None,
        fund_movement_rejected=None,
        withdrawal_rejected=None,
        post_revoke_order_rejected=None,
        operatorhub_bypass_disabled=operatorhub_bypass_disabled,
        perps_permission=authorization.perps_permission,
        fund_movement_path_absent=fund_movement_path_absent,
    )
    gate = authorize_pre_order_probe(
        policy=policy,
        evidence=evidence,
        now=authorization.block_timestamp,
        replay_protection_architecture_attestation=(
            replay_protection_architecture_attestation
        ),
    )
    assert_pre_order_probe_gate_attested(gate, request=request)
    return gate


async def _existing_execution(
    db: AsyncSession,
    job: CopyJob,
) -> Execution | None:
    return (
        await db.execute(
            select(Execution).where(
                Execution.copy_job_id == job.id,
                Execution.attempt_kind == 'o',
            )
        )
    ).scalar_one_or_none()


async def prepare_risex_worker_submission(
    db: AsyncSession,
    job: CopyJob,
    *,
    readiness_assertions: Mapping[str, bool],
) -> RISExWorkerPreparedSubmission | None:
    """Prepare one exact signed submission only when no durable execution exists."""

    network = str(job.execution_network or '')
    assert_risex_worker_write_allowed(
        network=network,  # type: ignore[arg-type]
        env=os.environ,
    )
    if network != 'testnet':
        raise SignedTestnetBlocked(
            'RISEx b2 signed worker transport is testnet-only until the mainnet gate PR'
        )

    # Durable restart fence: this query happens before b1, plan construction or
    # signing. Recovered ambiguity is reconciliation work, never a retry signal.
    existing = await _existing_execution(db, job)
    if existing is not None and existing.state in {
        ExecutionState.SUBMITTING,
        ExecutionState.UNKNOWN,
    }:
        return None
    if existing is not None:
        return None

    account_address = str(os.environ.get('RISEX_TESTNET_ACCOUNT_ADDRESS') or '').strip()
    if not account_address:
        raise SignedTestnetBlocked(
            'RISEX_TESTNET_ACCOUNT_ADDRESS is required for RISEx portfolio truth'
        )

    api = RISExReadOnlyHTTPTransport(base_url=_RISEX_TESTNET_API_URL)
    rpc = RISExReadOnlyRPCTransport(rpc_url=_RISEX_TESTNET_RPC_URL)
    transport: RISExSignedTestnetHTTPTransport | None = None
    try:
        markets_payload = await api.get_json('/v1/markets')
        portfolio_payload = await api.get_json(
            '/v1/portfolio/details',
            params={'account': account_address},
        )
        portfolio = parse_risex_portfolio_details(portfolio_payload)

        user = await db.get(User, job.user_id)
        risk = (
            await db.execute(
                select(RiskProfile).where(RiskProfile.user_id == job.user_id)
            )
        ).scalar_one_or_none()
        risk_state = (
            await db.execute(
                select(RiskState).where(RiskState.user_id == job.user_id)
            )
        ).scalar_one_or_none()
        if user is None or risk is None:
            raise SignedTestnetBlocked('RISEx user or RiskProfile is unavailable')

        ent = await entitlement(
            db,
            user,
            portfolio_equity_override=portfolio.total_account_value,
        )
        global_pause_flag = await db.get(SystemFlag, 'global_pause')
        emergency_stop_flag = await db.get(SystemFlag, 'emergency_stop')
        allowed_asset = (
            (not risk.allow_assets or job.asset in risk.allow_assets)
            and job.asset not in risk.block_assets
        )
        runtime = RISExRuntimeRiskFlags(
            user_active=user.state == UserState.ACTIVE,
            entitlement_active=bool(ent.get('entitled')),
            # Gate 3 readiness already proved the dedicated signer/session; the
            # request-specific gate below revalidates it before provider POST.
            credential_active=True,
            user_paused=user.copy_state == CopyState.PAUSED,
            global_pause=bool(global_pause_flag and global_pause_flag.enabled),
            emergency_stop=bool(emergency_stop_flag and emergency_stop_flag.enabled),
            drawdown_halt=bool(
                risk_state and risk_state.state == RiskHalt.DRAWDOWN_HALT
            ),
            daily_loss_halt=bool(
                risk_state and risk_state.state == RiskHalt.DAILY_LOSS_HALT
            ),
            near_liquidation=bool(risk_state and risk_state.near_liquidation),
            asset_allowed=allowed_asset,
            data_stale=False,
        )

        client_order_id = client_order_id_for_job(job.id, 'o')
        intent = plan_risex_order_intent(
            job=job,
            portfolio_payload=portfolio_payload,
            markets_payload=markets_payload,
            risk=risk,
            entitlement_data=ent,
            runtime=runtime,
            client_order_id=client_order_id,
        )
        plan = await prepare_risex_ioc_plan(api, intent)
        assert_risex_plan_matches_intent(intent=intent, plan=plan)

        request = await prepare_risex_ioc_request_from_plan(
            env=os.environ,
            api=api,
            rpc=rpc,
            plan=plan,
            network='testnet',
        )
        replay_attestation = await collect_replay_protection_architecture_attestation(
            api=api,
            rpc=rpc,
            request=request,
        )
        gate = await build_risex_pre_order_gate_for_request(
            env=os.environ,
            api=api,
            rpc=rpc,
            request=request,
            replay_protection_architecture_attestation=replay_attestation,
            disposable_account_asserted=readiness_assertions.get(
                'disposable_account_asserted'
            ) is True,
            dedicated_signer_asserted=readiness_assertions.get(
                'dedicated_signer_asserted'
            ) is True,
            operatorhub_bypass_disabled=readiness_assertions.get(
                'operatorhub_bypass_disabled'
            ) is True,
            fund_movement_path_absent=readiness_assertions.get(
                'fund_movement_path_absent'
            ) is True,
        )
        freshness_probe = make_freshness_probe(
            api=api,
            rpc=rpc,
            account_address=request.permit.account_address,
            signer_address=request.permit.signer_address,
            operatorhub_bypass_disabled=readiness_assertions.get(
                'operatorhub_bypass_disabled'
            ) is True,
            fund_movement_path_absent=readiness_assertions.get(
                'fund_movement_path_absent'
            ) is True,
        )
        transport = RISExSignedTestnetHTTPTransport(
            gate=gate,
            freshness_probe=freshness_probe,
        )

        cloid = deterministic_cloid(str(job.id), 'o')
        execution = await persist_risex_pre_post_execution(
            db,
            job=job,
            cloid=cloid,
            client_order_id=client_order_id,
            requested_size=plan.requested_size,
            limit_px=plan.limit_price,
            is_buy=intent.is_buy,
            reduce_only=intent.reduce_only,
            exposure_reader=lambda: (Decimal(0), Decimal(0)),
            user_exposure_ceiling=_RISEX_USER_EXPOSURE_CEILING_USDC,
            total_exposure_ceiling=_RISEX_TOTAL_EXPOSURE_CEILING_USDC,
            nonce_anchor=request.permit.nonce_anchor,
            nonce_bitmap_index=request.permit.nonce_bitmap_index,
        )

        if (
            execution.id is None
            or execution.cloid != cloid
            or int(execution.client_order_id or 0) != client_order_id
            or execution.nonce_anchor != request.permit.nonce_anchor
            or execution.nonce_bitmap_index != request.permit.nonce_bitmap_index
        ):
            raise SignedTestnetBlocked(
                'RISEx durable pre-POST execution does not match prepared submission'
            )

        submission = RISExPreparedCopySubmission(
            execution_id=execution.id,
            cloid=cloid,
            client_order_id=client_order_id,
            intent=intent,
            plan=plan,
            request=request,
        )
        return RISExWorkerPreparedSubmission(
            transport=transport,
            submission=submission,
            api=api,
            rpc=rpc,
        )
    except Exception:
        if transport is not None:
            await transport.aclose()
        await api.aclose()
        await rpc.aclose()
        raise
