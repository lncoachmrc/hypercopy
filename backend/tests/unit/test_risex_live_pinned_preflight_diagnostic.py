from __future__ import annotations

import asyncio
import json

from app.security.risex_deployment_preflight_cli import _run_preflight, build_parser


def test_capture_live_pinned_deployment_preflight() -> None:
    args = build_parser().parse_args([])
    payload, exit_code = asyncio.run(_run_preflight(args))
    raise AssertionError(
        'TRAXION_RISEX_PINNED_PREFLIGHT='
        + json.dumps({'exit_code': exit_code, 'payload': payload}, sort_keys=True)
    )
