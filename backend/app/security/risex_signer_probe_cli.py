from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from typing import Any, Sequence, cast

from app.adapters.risex_http import RISExReadOnlyHTTPTransport
from app.adapters.risex_types import ProviderReadUnavailable
from app.core.config import Network
from app.security.risex_signer_probe import (
    ProbeVerdict,
    RISExSignerCapabilityEvidence,
    RISExSignerCapabilityReport,
    collect_public_signer_evidence,
    evaluate_signer_capabilities,
)


_DEFAULT_BASE_URLS: dict[Network, str] = {
    'testnet': 'https://api.testnet.rise.trade',
    'mainnet': 'https://api.rise.trade',
}
_PUBLIC_ADDRESS_RE = re.compile(r'^0x[0-9a-fA-F]{40}$')


def public_address(value: str) -> str:
    """Accept an EVM public address only; private-key-shaped values are rejected."""

    if not _PUBLIC_ADDRESS_RE.fullmatch(value):
        raise argparse.ArgumentTypeError('expected a public EVM address (0x + 40 hex chars)')
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            'Collect public/read-only RISEx signer evidence and evaluate the TRAXION '
            'least-privilege security gate. This command never signs or mutates state.'
        )
    )
    parser.add_argument('--network', choices=('testnet', 'mainnet'), default='testnet')
    parser.add_argument('--account', required=True, type=public_address)
    parser.add_argument('--signer', required=True, type=public_address)
    parser.add_argument(
        '--base-url',
        default=None,
        help='Optional RISEx API base URL override. Public GET requests only.',
    )
    return parser


def report_payload(
    evidence: RISExSignerCapabilityEvidence,
    report: RISExSignerCapabilityReport,
) -> dict[str, Any]:
    return {
        'verdict': report.verdict,
        'security_gate_passed': report.security_gate_passed,
        'evidence': {
            'network': evidence.network,
            'account': evidence.account,
            'signer': evidence.signer,
            'chain_id': evidence.chain_id,
            'auth_contract': evidence.auth_contract,
            'router': evidence.router,
            'session_active': evidence.session_active,
            'session_account': evidence.session_account,
            'session_expiration': evidence.session_expiration,
            'onchain_perps_only_scope': evidence.onchain_perps_only_scope,
            'perps_order_succeeded': evidence.perps_order_succeeded,
            'fund_movement_rejected': evidence.fund_movement_rejected,
            'withdrawal_rejected': evidence.withdrawal_rejected,
            'post_revoke_order_rejected': evidence.post_revoke_order_rejected,
            'operatorhub_bypass_disabled': evidence.operatorhub_bypass_disabled,
        },
        'checks': [
            {
                'name': check.name,
                'verdict': check.verdict,
                'detail': check.detail,
            }
            for check in report.checks
        ],
        'writes_enabled': False,
    }


def verdict_exit_code(verdict: ProbeVerdict) -> int:
    if verdict == 'PASS':
        return 0
    if verdict == 'FAIL':
        return 1
    return 2


async def _run_probe(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    network = cast(Network, args.network)
    base_url = args.base_url or _DEFAULT_BASE_URLS[network]
    async with RISExReadOnlyHTTPTransport(base_url=base_url) as transport:
        evidence = await collect_public_signer_evidence(
            transport,
            network=network,
            account=args.account,
            signer=args.signer,
        )
    report = evaluate_signer_capabilities(evidence, now=int(time.time()))
    return report_payload(evidence, report), verdict_exit_code(report.verdict)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload, exit_code = asyncio.run(_run_probe(args))
    except ProviderReadUnavailable as exc:
        payload = {
            'verdict': 'UNKNOWN',
            'security_gate_passed': False,
            'read_error': str(exc),
            'writes_enabled': False,
        }
        exit_code = 2
    print(json.dumps(payload, indent=2, sort_keys=True))
    return exit_code
