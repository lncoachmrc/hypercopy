"""Deployment marker for the per-user TESTNET/MAINNET rollout.

Railway watches backend/app/core for API and worker services. This module is
intentionally side-effect free and exists to force a fresh source snapshot
after the Railway incident that interrupted the original deployment.
"""

NETWORK_SWITCH_ROLLOUT = "2026-08-19-v1"
