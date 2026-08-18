// Deployment marker for the per-user TESTNET/MAINNET rollout.
// Keeping this under /frontend ensures Railway creates a fresh source snapshot
// after the platform incident that interrupted the original deployment.
export const NETWORK_SWITCH_ROLLOUT = '2026-08-19-v1';
