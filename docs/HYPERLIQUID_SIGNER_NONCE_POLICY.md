# TRAXION Hyperliquid signer / nonce policy

## Scope

This policy covers every Hyperliquid **signed exchange action** performed with a
TRAXION follower API/Agent Wallet. It does not apply to read-only `/info` calls
or to the user's main wallet.

## Invariant

A single Hyperliquid API/Agent Wallet signer must never execute two signed
TRAXION actions concurrently from independent processes unless they share the
same distributed signer-coordination mechanism.

TRAXION currently enforces this invariant with
`app.db.signer_action_lock.signer_action_lock()`:

- the lock key is derived only from the public API-wallet address;
- PostgreSQL `pg_advisory_xact_lock` provides cross-process serialization;
- the lock spans the SDK action call, including SDK nonce generation/signing and
  the HTTP response;
- different signer addresses use different advisory keys and remain concurrent;
- database/lock failure is fail-closed: the signed action is not sent;
- private keys, seed phrases and main-wallet secrets are never lock inputs.

The signer lock solves same-database process serialization. It is not the
MAINNET writer authority across Railway environments: two environments may have
independent PostgreSQL/Redis while still controlling the same external wallet.
Therefore `_signed_call()` also enforces an environment-identity fence on every
MAINNET signed action. `RAILWAY_ENVIRONMENT_ID` must match the explicit
`TRAXION_MAINNET_WRITER_ENVIRONMENT_ID`; missing or mismatched identities are
rejected immediately before the synchronous SDK function with no exchange
action sent. TESTNET is unaffected.

Current signed adapter actions covered by the policy:

- `HyperliquidAdapter.update_leverage()`;
- `HyperliquidAdapter.place_ioc()`.

Any new signed Hyperliquid action must be added inside this same adapter-level
critical section before it is eligible for deployment. Instantiating or calling
`hyperliquid.exchange.Exchange` from a new application path without signer
serialization and the common MAINNET writer fence is prohibited.

## Scaling policy

The execution worker remains configured at one replica by default. Horizontal
scale-out does not remove or bypass the signer lock or MAINNET environment
fence. Before increasing worker replicas, the PostgreSQL multi-process
regression for HF-003 must remain green, and the real Hyperliquid
rate-budget/address-quota evidence required by the release plan must also be
reviewed.

Using a distinct API wallet per process is an acceptable future topology, but it
is not required by the current implementation and must not be introduced by
silently duplicating or sharing user credentials.

## Tests

- `tests/integration/test_hf003_signer_action_lock.py` starts independent Python
  processes against real CI PostgreSQL and proves that the same signer blocks
  while different signers proceed concurrently.
- `tests/unit/test_hf003_signed_action_path.py` proves every currently supported
  signed adapter action enters the signer lock using the public Agent Wallet
  address.
- `tests/unit/test_mainnet_single_writer_fence.py` proves TESTNET passthrough,
  MAINNET matching/missing/mismatched writer identity behavior, final boundary
  ordering, CLOSE_ALL-style IOC coverage and leverage/admin coverage.

No test in this policy sends a real Hyperliquid order.
