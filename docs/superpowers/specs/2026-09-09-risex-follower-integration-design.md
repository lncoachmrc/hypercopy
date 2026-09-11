# TRAXION RISEx Follower Integration Design

**Date:** 2026-09-09
**Status:** approved architecture, implementation gated
**Baseline:** `main` at `9e94b88dacee08354e7633a912efe0379bfb9451`

## Goal

Keep the TRAXION master source exclusively on Hyperliquid while allowing each follower to select Hyperliquid or RISEx as its execution destination. Strategy, proportional sizing, deterministic risk controls, auditability and reconciliation remain shared; exchange-specific behavior is isolated behind provider adapters.

## Non-negotiable constraints

- Master source remains Hyperliquid only.
- `execution_provider` and `execution_network` are independent dimensions.
- `key_provider` continues to mean encryption/key-wrapping provider and MUST NOT be reused for exchange selection.
- One execution provider is active per follower in v1.
- Main-wallet private keys and seed phrases are never accepted or stored.
- RISEx builder fee is disabled in v1.
- Provider switches never move collateral or migrate positions.
- A provider switch requires the strategy paused, verified-flat old destination, no open/conditional orders, no pending/retrying/processing jobs and no unresolved execution result.
- Historical rows retain the destination identity under which they were created.
- A job must bind immutably to provider, network, account, execution epoch, credential version and provider-local market identity.
- A runtime/provider outage must never trigger automatic fallback to another exchange.
- Errors reading account/position state must never be interpreted as zero positions.
- Hyperliquid behavior must remain regression-compatible while provider-neutral interfaces are introduced.
- The current integration work MUST NOT change `ENABLE_LIVE_TRADING`, database `live_trading`, or copy state of existing accounts.

## Operational identity

Introduce an immutable destination identity for execution work:

- `execution_provider`: `hyperliquid | risex`
- `execution_network`: `mainnet | testnet`
- `execution_epoch_id`: unique identifier for one operational configuration epoch
- trading account identity
- credential version/fingerprint
- provider-local market identity

Existing followers are backfilled as Hyperliquid while preserving their current network and state.

## Adapter architecture

The engine owns provider-neutral concepts. Exchange adapters own wire formats and exchange-specific constraints.

Shared types include:

- provider/network/destination identity
- account snapshot
- market capabilities/constraints
- position state
- order outcome
- provider order identifier mapping

Provider interface capabilities are introduced incrementally: market metadata, account snapshot, positions, open orders, fills, credential verification, leverage/margin state, IOC submit, cancellation and uncertain-result resolution.

Hyperliquid remains the first implementation. RISEx is introduced read-only before any write capability.

## Sizing

TRAXION keeps position targeting:

`master exposure ratio -> follower target notional -> follower target quantity -> delta vs real follower position`.

The source mark and destination mark are distinct. Exchange-specific minimum notional, quantity step, price tick/significant-figure rules and maximum leverage are supplied by the destination adapter. Hyperliquid's $10 floor and price rules MUST NOT become universal defaults for RISEx.

A reversal stays close-then-open. The second leg cannot be authorized until the first leg is positively reconciled as flat.

## RISEx API baseline verified 2026-09-09

Current developer documentation describes:

- signer registration with account `RegisterSigner` and signer `VerifySigner` EIP-712 signatures;
- bitmap nonces with indexes 0-207;
- signer status query and explicit revocation;
- order submission via `RISExUniversalRouter`;
- permit flow using `VerifyWitness(account,target,hash,nonceAnchor,nonceBitmap,deadline)`;
- IOC as `time_in_force = 3`;
- `client_order_id` as uint64;
- order/private-account WebSocket channels after session-key authentication;
- transaction decode endpoint for positive success/failure resolution;
- OperatorHub timed notional allowance for the JWT flow.

The API reference is explicitly under active development, so domains, router addresses, type hashes and contract addresses must be fetched/verified against runtime configuration rather than hardcoded.

## Security gate for RISEx writes

Current public API documentation proves registration, status, expiry and revocation of a session key but does not, by itself, prove that a stored session signer is cryptographically incapable of transfer/withdraw/fund-movement operations.

Therefore:

1. RISEx write capability is fail-closed by default.
2. Phases 1-3 may implement provider abstraction, persistence, dashboard readiness, public/private reads and simulation.
3. No RISEx order, leverage change, transfer, builder approval or other signed action may be reachable until a separate acceptance test proves the effective signer permissions on the active RISEx deployment.
4. Application endpoint blacklists are defense-in-depth only; they do not satisfy the cryptographic least-privilege requirement.
5. If RISEx cannot provide a suitably constrained delegation, automated follower execution remains blocked and requires a separate authorization design.

## Persistence and epochs

Use additive migrations. Do not recreate existing tables or resurrect deleted credentials/accounts.

The first migration adds provider/epoch identity while preserving existing data. Subsequent migrations can add provider-local identifiers and credential permission/readiness evidence.

Switching provider/network opens a new epoch rather than mutating historical execution identity. Current ledger/risk state for the new epoch is initialized separately after a verified-flat switch.

## Rollout phases

### Phase 0 — external verification

Verify current RISEx schemas, auth/domain, nonce model, revocation, read channels, transaction resolution and least-privilege capability. The least-privilege write gate remains open as a blocker.

### Phase 1 — provider-neutral foundation

Extract common types/interfaces and destination identity while preserving existing Hyperliquid behavior. No RISEx write path exists.

### Phase 2 — persistence and dashboard

Add provider + epoch persistence, safe switch semantics, readiness reporting and UI selection. RISEx remains non-executable.

### Phase 3 — RISEx reads and simulation

Implement system/market metadata, portfolio/positions/orders/fills reads, account readiness and target simulation using RISEx destination prices/constraints.

### Phase 4 — testnet writes

Only after the security gate passes: implement signed actions, nonce coordination, ambiguous-result recovery, partial fills, cancellation, leverage and reversal tests on an isolated test environment.

### Phase 5 — controlled release

Requires explicit authorization after staging/test environment verification, CI, security checks, rollback validation and mainnet gates.

## Railway constraint discovered 2026-09-09

The connected Railway project `HyperCopy` currently exposes only an environment named `production`; no `staging` environment is present. Existing trading services are healthy, but their deployed commits predate the landing-only merge because watch paths correctly skipped the unrelated commit.

No implementation branch may be deployed to the current `production` environment as part of phases 1-3. Before runtime validation, create or identify an isolated staging/test environment and verify its service-level variables and database isolation.

## Acceptance criteria for this first milestone

- Hyperliquid tests stay green.
- Provider/network are separate typed dimensions.
- Existing users resolve to Hyperliquid without behavior change.
- Destination identity includes an epoch and is bindable to jobs/executions without relying only on network.
- Sizing accepts provider-specific market constraints instead of forcing Hyperliquid minimum/rounding globally.
- RISEx adapter can perform only read/simulation capabilities.
- Any attempt to invoke an unapproved RISEx write capability fails locally before signing/network submission.
- No production Railway deployment or mainnet action occurs.
