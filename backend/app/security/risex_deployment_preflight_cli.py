from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any, Sequence, cast

from app.adapters.risex_http import RISExReadOnlyHTTPTransport
from app.adapters.risex_types import ProviderDataMalformed, ProviderReadUnavailable
from app.security.risex_deployment_preflight import evaluate_pinned_deployment_preflight
from app.security.risex_deployment_probe import Verdict
from app.security.risex_deployment_runtime import (
    PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT,
    RISExReadOnlyRPCTransport,
    collect_runtime_deployment_evidence,
)


_DEFAULT_API_URL = 'https://api.testnet.rise.trade'
_DEFAULT_RPC_URL = 'https://testnet.riselabs.xyz'


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            'Run the TRAXION RISEx testnet deployment identity preflight using '
            'public/read-only API and RPC evidence only. This command has no write path.'
        )
    )
    parser.add_argument('--network', choices=('testnet',), default='testnet')
    parser.add_argument(
        '--base-url',
        default=_DEFAULT_API_URL,
        help='RISEx public API base URL override; GET requests only.',
    )
    parser.add_argument(
        '--rpc-url',
        default=_DEFAULT_RPC_URL,
        help='RISE testnet JSON-RPC URL override; deployment read methods only.',
    )
    return parser


def verdict_exit_code(verdict: Verdict) -> int:
    if verdict == 'PASS':
        return 0
    if verdict == 'FAIL':
        return 1
    return 2


def _contract_payload(contract: object) -> dict[str, Any]:
    return {
        'address': getattr(contract, 'address', None),
        'runtime_code_bytes': getattr(contract, 'runtime_code_bytes', None),
        'runtime_code_keccak256': getattr(contract, 'runtime_code_keccak256', None),
        'implementation': getattr(contract, 'implementation', None),
        'implementation_code_bytes': getattr(contract, 'implementation_code_bytes', None),
        'implementation_code_keccak256': getattr(
            contract,
            'implementation_code_keccak256',
            None,
        ),
        'abi_verified': getattr(contract, 'abi_verified', None),
    }


async def _run_preflight(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    network = cast(str, args.network)
    async with RISExReadOnlyHTTPTransport(base_url=args.base_url) as api:
        async with RISExReadOnlyRPCTransport(rpc_url=args.rpc_url) as rpc:
            evidence = await collect_runtime_deployment_evidence(
                api,
                rpc,
                network=network,
            )

    report = evaluate_pinned_deployment_preflight(
        evidence,
        expected_fingerprint=PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT,
    )
    payload: dict[str, Any] = {
        'verdict': report.verdict,
        'deployment_identity_verified': report.deployment_identity_verified,
        'expected_fingerprint': report.expected_fingerprint,
        'observed_fingerprint': report.observed_fingerprint,
        'evidence': {
            'network': evidence.network,
            'api_chain_id': evidence.api_chain_id,
            'rpc_chain_id': evidence.rpc_chain_id,
            'block_number': evidence.block_number,
            'domain_name': evidence.domain_name,
            'domain_version': evidence.domain_version,
            'domain_verifying_contract': evidence.domain_verifying_contract,
            'system_auth_contract': evidence.system_auth_contract,
            'system_router': evidence.system_router,
            'authorization': _contract_payload(evidence.auth),
            'router': _contract_payload(evidence.router),
        },
        'checks': [
            {
                'name': check.name,
                'verdict': check.verdict,
                'detail': check.detail,
            }
            for check in report.checks
        ],
        'full_security_gate_passed': False,
        'writes_enabled': False,
    }
    return payload, verdict_exit_code(report.verdict)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload, exit_code = asyncio.run(_run_preflight(args))
    except (ProviderReadUnavailable, ProviderDataMalformed) as exc:
        payload = {
            'verdict': 'UNKNOWN',
            'deployment_identity_verified': False,
            'read_error': str(exc),
            'full_security_gate_passed': False,
            'writes_enabled': False,
        }
        exit_code = 2
    print(json.dumps(payload, indent=2, sort_keys=True))
    return exit_code
