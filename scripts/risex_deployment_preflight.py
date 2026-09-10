#!/usr/bin/env python3
"""Run the TRAXION RISEx testnet deployment identity preflight."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'backend'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.security.risex_deployment_preflight_cli import main  # noqa: E402


if __name__ == '__main__':
    raise SystemExit(main())
