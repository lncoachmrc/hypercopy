#!/usr/bin/env python3
"""Place exactly one explicitly approved RISEx BTC testnet probe order.

The CLI is intentionally fail-closed: approval defaults OFF, the intent is fixed
to BTC/BUY/minimum-size, the process may consume the execution path only once,
and diagnostics never include private-key, signature or permit material.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'backend'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.adapters.risex_http import RISExReadOnlyHTTPTransport  # noqa: E402
from app.adapters.risex_signed_testnet_http import (  # noqa: E402
    RISExSignedTestnetHTTPTransport,
)
from app.adapters.risex_types import (  # noqa: E402
    ProviderDataMalformed,
    ProviderReadUnavailable,
    ProviderWriteDisabled,
)
from app.security.risex_authorization_session import (  # noqa: E402
    collect_authorization_session_evidence,
)
from app.security.risex_deployment_preflight import (  # noqa: E402
    evaluate_pinned_deployment_preflight,
)
from app.security.risex_deployment_runtime import (  # noqa: E402
    PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT,
    RISExReadOnlyRPCTransport,
    collect_runtime_deployment_evidence,
)
from app.security.risex_pre_order_gate import authorize_pre_order_probe  # noqa: E402
from app.security.risex_replay_protection_architecture import (  # noqa: E402
    RISExReplayProtectionArchitectureAttestation,
    collect_replay_protection_architecture_attestation,
)
from app.security.risex_signed_testnet_policy import (  # noqa: E402
    SignedTestnetBlocked,
    SignedTestnetPolicy,
)
from app.security.risex_signer_probe import RISExSignerCapabilityEvidence  # noqa: E402
from app.security.risex_testnet_signer import load_testnet_signer_credential  # noqa: E402
from app.services.risex_order_preparation import (  # noqa: E402
    make_freshness_probe,
    prepare_risex_ioc_request,
)
from app.services.risex_signed_execution import (  # noqa: E402
    arm_risex_signed_testnet_execution,
)


_DEFAULT_API_URL = 'https://api.testnet.rise.trade'
_DEFAULT_RPC_URL = 'https://testnet.riselabs.xyz'
_EXECUTION_CONSUMED = False
_SENSITIVE_KEY_FRAGMENTS = (
    'private_key',
    'signature',
    'seed',
    'secret',
)


def manual_probe_intent() -> SimpleNamespace:
    """Return the immutable manual probe intent; market id is resolved live."""

    return SimpleNamespace(
        symbol='BTC',
        side='BUY',
        use_min_order_size=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            'Prepare and, only with explicit operator approval, submit one BTC/BUY '
            'minimum-size RISEx testnet IOC. No retries are performed.'
        )
    )
    parser.add_argument(
        '--approve-testnet-order',
        action='store_true',
        default=False,
        help='Explicitly authorize exactly one RISEx testnet order attempt.',
    )
    parser.add_argument('--base-url', default=_DEFAULT_API_URL)
    parser.add_argument('--rpc-url', default=_DEFAULT_RPC_URL)
    parser.add_argument('--slippage-bps', type=int, default=25)
    parser.add_argument('--deadline-seconds', type=int, default=30)
    parser.add_argument(
        '--disposable-account-asserted',
        action='store_true',
        help='Assert that the configured RISEx account is disposable/test-only.',
    )
    parser.add_argument(
        '--dedicated-signer-asserted',
        action='store_true',
        help='Assert that the configured signer is dedicated to this testnet probe.',
    )
    parser.add_argument(
        '--operatorhub-bypass-disabled',
        action='store_true',
        help='Assert that JWT/OperatorHub bypass remains disabled.',
    )
    parser.add_argument(
        '--fund-movement-path-absent',
        action='store_true',
        help='Assert the reviewed ADR-0002 fund-movement-path absence.',
    )
    return parser


def _sensitive_key(name: object) -> bool:
    if not isinstance(name, str):
        return False
    lowered = name.lower()
    if lowered == 'permit':
        return True
    return any(fragment in lowered for fragment in _SENSITIVE_KEY_FRAGMENTS)


def sanitize_diagnostic_payload(value: Any) -> Any:
    """Recursively remove all secret/signature/permit material from diagnostics."""

    if isinstance(value, dict):
        return {
            key: sanitize_diagnostic_payload(item)
            for key, item in value.items()
            if not _sensitive_key(key)
        }
    if isinstance(value, list):
        return [sanitize_diagnostic_payload(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_diagnostic_payload(item) for item in value]
    return value


def _claim_one_shot_execution() -> None:
    global _EXECUTION_CONSUMED
    if _EXECUTION_CONSUMED:
        raise SignedTestnetBlocked(
            'RISEx manual testnet probe is one-shot per process; execution was already consumed'
        )
    _EXECUTION_CONSUMED = True


async def _build_pre_order_gate(
    *,
    env: os._Environ[str],
    api: RISExReadOnlyHTTPTransport,
    rpc: RISExReadOnlyRPCTransport,
    disposable_account_asserted: bool,
    dedicated_signer_asserted: bool,
    replay_protection_architecture_attestation: RISExReplayProtectionArchitectureAttestation,
    operatorhub_bypass_disabled: bool,
    fund_movement_path_absent: bool,
) -> object:
    credential = load_testnet_signer_credential(env)
    deployment = await collect_runtime_deployment_evidence(
        api,
        rpc,
        network='testnet',
    )
    deployment_report = evaluate_pinned_deployment_preflight(
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
        deployment_verdict=deployment_report.verdict,
        deployment_identity_verified=deployment_report.deployment_identity_verified,
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
    return authorize_pre_order_probe(
        policy=policy,
        evidence=evidence,
        now=authorization.block_timestamp,
        replay_protection_architecture_attestation=replay_protection_architecture_attestation,
    )


async def _execute_once(args: argparse.Namespace) -> dict[str, Any]:
    """Execute at most one signed testnet POST. There is deliberately no retry loop."""

    _claim_one_shot_execution()
    if os.environ.get('RISEX_SIGNED_WRITES_ENABLED') != 'true':
        raise SignedTestnetBlocked(
            'RISEX_SIGNED_WRITES_ENABLED must be explicitly true for the manual probe'
        )

    required_assertions = (
        args.disposable_account_asserted,
        args.dedicated_signer_asserted,
        args.operatorhub_bypass_disabled,
        args.fund_movement_path_absent,
    )
    if not all(required_assertions):
        raise SignedTestnetBlocked(
            'RISEx manual probe requires all explicit testnet safety assertions'
        )

    intent = manual_probe_intent()
    async with RISExReadOnlyHTTPTransport(base_url=args.base_url) as api:
        async with RISExReadOnlyRPCTransport(rpc_url=args.rpc_url) as rpc:
            request = await prepare_risex_ioc_request(
                env=os.environ,
                api=api,
                rpc=rpc,
                symbol=intent.symbol,
                side=intent.side,
                use_min_order_size=intent.use_min_order_size,
                slippage_bps=args.slippage_bps,
                deadline_seconds=args.deadline_seconds,
            )
            replay_architecture = await collect_replay_protection_architecture_attestation(
                api=api,
                rpc=rpc,
                request=request,
            )
            gate = await _build_pre_order_gate(
                env=os.environ,
                api=api,
                rpc=rpc,
                disposable_account_asserted=args.disposable_account_asserted,
                dedicated_signer_asserted=args.dedicated_signer_asserted,
                replay_protection_architecture_attestation=replay_architecture,
                operatorhub_bypass_disabled=args.operatorhub_bypass_disabled,
                fund_movement_path_absent=args.fund_movement_path_absent,
            )
            freshness_probe = make_freshness_probe(
                api=api,
                account_address=request.permit.account_address,
                signer_address=request.permit.signer_address,
            )

            async with await arm_risex_signed_testnet_execution(
                env=os.environ,
                api=api,
                rpc=rpc,
                pre_order_gate=gate,  # type: ignore[arg-type]
                freshness_probe=freshness_probe,
                explicit_approval=True,
                disposable_account_asserted=args.disposable_account_asserted,
                dedicated_signer_asserted=args.dedicated_signer_asserted,
                operatorhub_bypass_disabled=args.operatorhub_bypass_disabled,
                fund_movement_path_absent=args.fund_movement_path_absent,
            ) as session:
                transport = session.adapter.transport
                if not isinstance(transport, RISExSignedTestnetHTTPTransport):
                    raise SignedTestnetBlocked(
                        'RISEx manual probe requires the signed testnet transport'
                    )
                provider_result = await transport.post_place_order(request)

    return sanitize_diagnostic_payload(
        {
            'submitted': True,
            'network': 'testnet',
            'symbol': intent.symbol,
            'side': intent.side,
            'market_id': request.order.market_id,
            'size_steps': request.order.size_steps,
            'price_ticks': request.order.price_ticks,
            'client_order_id': request.order.client_order_id,
            'provider_response': provider_result,
        }
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.approve_testnet_order:
        print(
            json.dumps(
                {
                    'submitted': False,
                    'post_attempted': False,
                    'reason': 'explicit --approve-testnet-order flag is required',
                },
                sort_keys=True,
            )
        )
        return 2

    try:
        payload = asyncio.run(_execute_once(args))
    except (
        ProviderDataMalformed,
        ProviderReadUnavailable,
        ProviderWriteDisabled,
        SignedTestnetBlocked,
    ) as exc:
        payload = {
            'submitted': False,
            'post_attempted': False,
            'error_type': type(exc).__name__,
        }
        print(json.dumps(sanitize_diagnostic_payload(payload), sort_keys=True))
        return 2

    print(json.dumps(sanitize_diagnostic_payload(payload), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
