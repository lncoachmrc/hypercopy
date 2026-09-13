#!/usr/bin/env python3
"""Run the TRAXION RISEx signed-testnet readiness probe without provider writes."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'backend'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.adapters.risex_http import RISExReadOnlyHTTPTransport  # noqa: E402
from app.adapters.risex_types import (  # noqa: E402
    ProviderDataMalformed,
    ProviderReadUnavailable,
)
from app.security.risex_deployment_runtime import RISExReadOnlyRPCTransport  # noqa: E402
from app.security.risex_signed_testnet_policy import SignedTestnetBlocked  # noqa: E402
from app.security.risex_signed_testnet_runner import (  # noqa: E402
    RISExSignedTestnetReadinessReport,
    run_signed_testnet_readiness,
)


_DEFAULT_API_URL = 'https://api.testnet.rise.trade'
_DEFAULT_RPC_URL = 'https://testnet.riselabs.xyz'


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            'Collect RISEx signed-testnet readiness evidence using read-only API/RPC '
            'operations. This command does not enable provider writes.'
        )
    )
    parser.add_argument('--network', choices=('testnet',), default='testnet')
    parser.add_argument(
        '--base-url',
        default=_DEFAULT_API_URL,
        help='RISEx public testnet API base URL; GET requests only.',
    )
    parser.add_argument(
        '--rpc-url',
        default=_DEFAULT_RPC_URL,
        help='RISE testnet JSON-RPC URL; read-only RPC methods only.',
    )
    parser.add_argument(
        '--explicit-approval',
        action='store_true',
        help='Assert explicit approval for the signed-testnet readiness probe.',
    )
    parser.add_argument(
        '--disposable-account-asserted',
        action='store_true',
        help='Assert that the configured RISEx account is disposable/test-only.',
    )
    parser.add_argument(
        '--dedicated-signer-asserted',
        action='store_true',
        help='Assert that the configured signer is dedicated to this testnet verification.',
    )
    parser.add_argument(
        '--operatorhub-bypass-disabled',
        action='store_true',
        help='Assert that the OperatorHub bypass path is disabled.',
    )
    parser.add_argument(
        '--fund-movement-path-absent',
        action='store_true',
        default=False,
        help=(
            'Explicitly assert the reviewed ADR-0002 assumption that no documented '
            'fund-movement path accepts the RISEx session-key signature. Disabled by '
            'default; this condition is never inferred from code.'
        ),
    )
    return parser


def _exit_code(report: RISExSignedTestnetReadinessReport) -> int:
    if report.verdict == 'PASS':
        return 0
    if report.verdict == 'FAIL':
        return 1
    return 2


async def _run(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    async with RISExReadOnlyHTTPTransport(base_url=args.base_url) as api:
        async with RISExReadOnlyRPCTransport(rpc_url=args.rpc_url) as rpc:
            report = await run_signed_testnet_readiness(
                env=os.environ,
                api=api,
                rpc=rpc,
                network='testnet',
                explicit_approval=args.explicit_approval,
                disposable_account_asserted=args.disposable_account_asserted,
                dedicated_signer_asserted=args.dedicated_signer_asserted,
                operatorhub_bypass_disabled=args.operatorhub_bypass_disabled,
                fund_movement_path_absent=args.fund_movement_path_absent,
            )
    return asdict(report), _exit_code(report)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload, exit_code = asyncio.run(_run(args))
    except (ProviderReadUnavailable, ProviderDataMalformed, SignedTestnetBlocked) as exc:
        payload = {
            'verdict': 'UNKNOWN',
            'adr_reference': 'ADR-0002',
            'fund_movement_path_absent': bool(args.fund_movement_path_absent),
            'readiness_error': str(exc),
            'post_allowed': False,
            'full_security_gate_passed': False,
            'writes_enabled': False,
        }
        exit_code = 2
    print(json.dumps(payload, indent=2, sort_keys=True))
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
