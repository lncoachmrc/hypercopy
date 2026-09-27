from __future__ import annotations

from app.adapters.risex_http import RISExReadOnlyHTTPTransport
from app.security.risex_authorization_session import (
    RISExAuthorizationSessionEvidence,
    collect_authorization_session_evidence,
)
from app.security.risex_deployment_preflight import evaluate_pinned_deployment_preflight
from app.security.risex_deployment_runtime import (
    PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT,
    RISExReadOnlyRPCTransport,
    collect_runtime_deployment_evidence,
)


_RISEX_TESTNET_API_URL = 'https://api.testnet.rise.trade'
_RISEX_TESTNET_RPC_URL = 'https://testnet.riselabs.xyz'


async def _verify_risex_signer_binding(
    *,
    account_address: str,
    signer_address: str,
) -> RISExAuthorizationSessionEvidence:
    async with RISExReadOnlyHTTPTransport(
        base_url=_RISEX_TESTNET_API_URL
    ) as api:
        async with RISExReadOnlyRPCTransport(
            rpc_url=_RISEX_TESTNET_RPC_URL
        ) as rpc:
            deployment = await collect_runtime_deployment_evidence(
                api,
                rpc,
                network='testnet',
            )
            preflight = evaluate_pinned_deployment_preflight(
                deployment,
                expected_fingerprint=PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT,
            )
            if (
                preflight.verdict != 'PASS'
                or preflight.deployment_identity_verified is not True
            ):
                raise RuntimeError('RISEx deployment identity is not verified')

            evidence = await collect_authorization_session_evidence(
                rpc,
                authorization_address=deployment.domain_verifying_contract,
                account=account_address,
                signer=signer_address,
                block_tag=hex(deployment.block_number),
            )

    if evidence.account.lower() != account_address.lower():
        raise RuntimeError('RISEx authorization account binding mismatch')
    if evidence.signer.lower() != signer_address.lower():
        raise RuntimeError('RISEx authorization signer binding mismatch')
    if evidence.session_active is not True:
        raise RuntimeError('RISEx signer session is not active')
    if evidence.session_not_expired is not True:
        raise RuntimeError('RISEx signer session is expired')
    if evidence.perps_permission is not True:
        raise RuntimeError('RISEx signer lacks required Perps permission')
    return evidence
