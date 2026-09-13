# TRAXION RISEx Provider Selection and Safe-Switch Design

**Date:** 2026-09-13  
**Status:** draft for owner approval  
**Baseline:** `main` at `0db51ba44d4fc2307b62a1069291c59a31d7acd5`  
**Parent architecture:** `docs/superpowers/specs/2026-09-09-risex-follower-integration-design.md`

## Goal

Complete the missing Phase 2 provider-selection path so an authenticated follower can explicitly choose `hyperliquid` or `risex` while preserving the execution-epoch model and the original safe-switch semantics.

This work is not an execution-routing change. It creates and protects the destination identity that later execution code consumes. Hyperliquid remains the default provider. Selecting RISEx does not auto-enable trading, does not auto-create a RISEx credential, does not bypass signed-write readiness, and does not route ordinary worker traffic to RISEx.

The immediate test-environment path that must become possible is:

1. wallet SIWE login on an empty database;
2. new user is created with the existing default `hyperliquid` provider and configured follower network;
3. the user explicitly pauses the strategy;
4. the owner-scoped provider endpoint selects `risex`;
5. the old initial Hyperliquid epoch is closed;
6. a new RISEx epoch is created on the same network;
7. no direct SQL, mock, monkeypatch or implicit provider default is involved.

## Existing architecture that must be preserved

The current repository already provides:

- `users.execution_provider` with default/backfill `hyperliquid`;
- `users.execution_network` as an independent dimension;
- immutable `execution_epochs`;
- `set_user_destination()` as the single function that closes an existing epoch and opens a new one;
- immutable job/execution destination binding;
- stale-epoch rejection at execution time;
- `PUT /trading-network` with local switch blockers;
- Hyperliquid account-state reads;
- a sealed RISEx signed-testnet execution path whose write authorization remains independent of provider selection.

The missing Phase 2 capability is an application path that lets the authenticated owner select the provider and creates the corresponding epoch safely.

## Non-negotiable safe-switch semantics

The 2026-09-09 architecture requires all of the following before a provider/network switch:

- strategy is `PAUSED`;
- previous destination is verified flat, or is provably never activated as defined below;
- no open or conditional orders exist on the previous destination;
- no `QUEUED`, `PROCESSING` or `RETRYING` jobs remain for the current destination epoch;
- no unresolved execution result remains;
- errors reading provider state are never interpreted as zero positions or zero orders;
- the switch never moves collateral and never migrates positions;
- historical rows retain their original provider/network/epoch identity;
- a successful provider/network change opens a new epoch and never mutates the old epoch in place.

These are correctness and authorization invariants, not UI checks.

## Decision 1 — the authoritative guard lives inside `set_user_destination()`

### Problem

Today `_network_switch_status()` protects `PUT /trading-network`, but `set_user_destination()` itself can close the current epoch and open another without checking pause, positions, pending jobs or unresolved executions. A future caller can therefore omit the endpoint-level precheck and still perform the state transition.

Provider selection would make that omission reachable from a new application path.

### Decision

`set_user_destination()` becomes the authoritative destination-transition boundary.

Whenever the target `provider` or `network` differs from the user's persisted current destination identity, `set_user_destination()` MUST execute the safe-switch guard itself before closing or replacing any epoch.

The guard is not satisfied by a caller-provided boolean such as `safe=True`, and callers do not supply a forgeable `VERIFIED_FLAT` object. `set_user_destination()` owns the classification and invokes the provider verifier itself.

The function MUST distinguish these cases:

1. **Initial materialization** — no previous operational destination has ever existed. The existing Hyperliquid default/bootstrap remains valid without a switch check.
2. **No-op** — target provider/network equals the current provider/network and the existing destination identity is otherwise unchanged. Return the current state without creating a new epoch.
3. **Provider/network switch** — target provider or network differs. Full safe-switch semantics apply.
4. **Same-provider credential/account rebind** — provider/network are unchanged but account/credential identity changes. This is not redefined as a provider switch by this work; existing account-link safety behavior remains separate unless the implementation plan finds a direct conflict that must be handled to preserve this design.

### Close-before-switch bypass resistance

The guard MUST NOT depend only on `active_execution_epoch_id` being non-null.

If another application path closes the active epoch first, `users.execution_provider` and `users.execution_network` still identify the previous persisted destination. `set_user_destination()` MUST compare the target to those persisted values and inspect prior epoch history. A caller cannot turn a provider/network change into an apparent bootstrap merely by calling `close_user_destination_epoch()` first.

An unguarded creation is allowed only for genuine first-time materialization, not for a user with a prior destination history whose target provider/network differs from the persisted identity.

### Transaction and freshness ordering

A provider/network switch is rare, so correctness is preferred over minimizing the duration of a single-user transaction.

`set_user_destination()` MUST:

1. lock/read the user destination identity;
2. evaluate DB-local blockers;
3. classify `NEVER_ACTIVATED` or perform the provider-side verification;
4. re-check DB-local blockers after the provider read and before mutation;
5. confirm the source epoch/provider/network identity still matches what was verified;
6. only then close the old epoch and create the new epoch.

No provider write occurs during this process.

A provider-read timeout, malformed response, unsupported read capability or identity mismatch becomes `UNREADABLE` and blocks the switch.

## Decision 2 — three previous-destination states

The previous destination MUST be classified as exactly one of these states.

### `VERIFIED_FLAT`

Use only when all required provider reads succeed and prove the previous operational destination is empty.

Requirements:

- source account identity is known and matches the source epoch;
- provider position/account-state read succeeds;
- every position size is zero;
- provider open-order read succeeds;
- regular open orders are empty;
- conditional/trigger orders are empty;
- local managed `PositionLedger` contains no non-zero managed position;
- all DB-local pause/job/execution blockers are clear.

A partial read is not enough. A successful position read combined with an order-read failure is `UNREADABLE`, not `VERIFIED_FLAT`.

### `NEVER_ACTIVATED`

This is a database-proven lifecycle state, not a synonym for flat.

It is valid only when all of the following are true for the user:

- no `TradingAccount` exists;
- no `SigningCredential` exists;
- no historical `ExecutionEpoch` has a non-null `account_address`;
- local managed position ledger has no non-zero position;
- no pending/retrying/processing job exists for the current destination;
- no unresolved execution exists for the current destination;
- strategy is `PAUSED`.

Because no provider account was ever bound to an epoch, there is no provider account to query. The absence of an account is positive lifecycle evidence here, not a failed provider read.

The expected empty-database path therefore becomes:

`wallet login -> hyperliquid/testnet epoch with account_address=NULL -> PAUSE -> select risex -> NEVER_ACTIVATED -> new risex/testnet epoch`.

### `UNREADABLE`

This state blocks the switch in every case.

Examples include:

- provider position read fails or times out;
- provider order read fails or returns malformed/partial data;
- source account identity is missing even though activation history exists;
- source epoch identity and stored account identity disagree;
- a provider lacks the required read implementation;
- any required state is unknown or ambiguous.

`UNREADABLE` MUST never be mapped to `NEVER_ACTIVATED` or `VERIFIED_FLAT`.

## Decision 3 — close the current safe-switch gap in this PR, fail closed where a provider read does not yet exist

The new endpoint must not be released with the weaker current `_network_switch_status()` semantics as its final authorization boundary.

This PR therefore closes the semantic gap now rather than deferring the requirement:

### Hyperliquid as previous destination

The implementation MUST perform live provider-side verification in this PR.

It must use Hyperliquid account state to prove zero positions and a Hyperliquid open-order read that includes trigger/conditional orders. The existing adapter may gain the minimal read wrapper required for this verification. The switch verifier must treat any read error as `UNREADABLE`.

The implementation should prefer the Hyperliquid `frontendOpenOrders` semantics because the response identifies trigger/TP/SL orders explicitly; an empty response proves there are no regular or conditional orders represented by that endpoint.

### RISEx as previous destination

The current repository does not yet expose a complete authenticated/private RISEx account-state + open/conditional-order read path suitable for proving an activated RISEx account flat.

The provider-selection PR MUST NOT invent or weaken that evidence.

Therefore:

- an unactivated RISEx destination may switch via `NEVER_ACTIVATED`;
- an activated RISEx destination without complete position + order verification is `UNREADABLE` and cannot switch provider/network;
- enabling `VERIFIED_FLAT` for an activated RISEx destination is a separate read-capability follow-up, not a relaxation in this PR.

This means the safe-switch contract is complete in this PR even though one provider remains fail-closed for the activated case.

## Decision 4 — DB-local blockers move to the shared destination-transition layer

The current `_network_switch_status()` logic is useful but endpoint-local and incomplete.

A shared destination-switch precheck MUST become the single source for DB-local blockers used by both provider and network changes.

At minimum it checks:

- `copy_state == PAUSED`;
- no non-zero managed `PositionLedger` row;
- no current-epoch `CopyJob` in `QUEUED`, `PROCESSING` or `RETRYING`;
- no current-epoch `Execution` in unresolved states (`SUBMITTING` or `UNKNOWN`; any later unresolved state added to the model must also be treated as blocking).

The mutation boundary inside `set_user_destination()` re-evaluates these conditions authoritatively. API/UI readiness may reuse the same helper for presentation but cannot substitute for the final check.

## Decision 5 — owner-scoped provider endpoint

Add an authenticated endpoint symmetric with the network selector:

`PUT /api/v1/trading-provider`

Request body:

```json
{
  "provider": "hyperliquid" | "risex"
}
```

Rules:

- `provider` is required; omission does not imply RISEx;
- there is no `user_id` in the request body or path;
- the endpoint uses `current_user` and therefore can mutate only the authenticated owner's destination;
- CSRF protection is required;
- master-source users remain excluded from follower controls;
- selecting the already-active provider is idempotent and creates no new epoch;
- changing provider preserves the current `execution_network`;
- a successful provider switch leaves strategy state `PAUSED`;
- it never auto-resumes or auto-enables signed execution;
- it calls `set_user_destination()` and relies on the central guard rather than reproducing authorization logic in the endpoint.

No endpoint may directly update `users.execution_provider` or `active_execution_epoch_id`.

## Epoch and state-transition behavior

For an allowed provider/network switch:

1. the old epoch is closed by setting `ended_at`;
2. the old epoch's provider, network, account and credential identity remain unchanged;
3. a new UUID is generated;
4. a new `execution_epochs` row is inserted with the target provider/network;
5. `users.execution_provider`, `execution_network`, `network_started_at` and `active_execution_epoch_id` are updated to the new epoch;
6. stale old-epoch jobs remain stale and fail existing epoch fences;
7. no historical job/execution row is rewritten.

For a provider switch to RISEx before a user-specific RISEx account/credential is linked, the new epoch may legitimately have `account_address=NULL` and `credential_version=NULL`. Provider selection is destination intent; it is not proof of executable readiness.

## Credential and runtime-state cleanup ordering

The previous destination must be verified before destructive local cleanup.

The current network-switch endpoint deletes the `TradingAccount` and local ledger/risk state before calling `set_user_network()`. The implementation plan MUST reorder that path so the central switch guard can inspect the authoritative previous destination before any account evidence is destroyed.

For provider/network switches:

- safe-switch verification happens first;
- epoch transition happens only after verification succeeds;
- provider-specific credential cleanup and local ledger/risk reset happen after the transition decision within the same database transaction;
- if any database step fails before commit, the whole local transition rolls back;
- no provider-side write is part of the switch.

Switching away from Hyperliquid removes the old Hyperliquid `TradingAccount`/`SigningCredential` only after the safe-switch decision succeeds. Returning to Hyperliquid requires linking a fresh dedicated API wallet through the existing Hyperliquid account-link flow.

## Readiness reporting and UI selection

Phase 2 also requires provider selection to be visible to the user.

`/me` / dashboard serialization should expose at least:

- current `execution_provider`;
- current `execution_network`;
- local destination-switch readiness;
- structured local blockers.

The UI may display local readiness continuously, but it MUST NOT claim the previous destination is provider-verified flat based only on local state.

Provider-side flat/order verification happens on the actual switch request because it must be fresh and because polling provider state every few seconds would create unnecessary rate-limit load and stale authorization evidence.

Settings adds an explicit `Hyperliquid / RISEx` provider selector independent from `TESTNET / MAINNET`.

UI behavior:

- Hyperliquid remains selected by default for new users;
- a different provider cannot be selected while local blockers exist;
- the switch action tells the user that live destination verification is performed at confirmation time;
- a provider-read failure is shown as a blocked switch, never as flat;
- selecting RISEx does not present it as trading-ready unless the separate RISEx readiness/signer gates pass;
- existing Hyperliquid API-wallet fields must not be mislabeled as RISEx credentials.

## Empty-database SIWE path

The isolated `api Copy` service is already configured so wallet SIWE can create the first user on its empty database.

The application flow after this work is:

1. `POST /api/v1/auth/challenge`;
2. wallet signs the SIWE message;
3. `POST /api/v1/auth/verify`;
4. backend creates `User`, risk rows, trial subscription and the initial destination epoch;
5. initial provider remains `hyperliquid` by schema/application default;
6. configured follower network is used (`testnet` on `api Copy`);
7. user explicitly pauses;
8. `PUT /api/v1/trading-provider {"provider":"risex"}`;
9. `set_user_destination()` proves `NEVER_ACTIVATED` and creates a new RISEx/testnet epoch.

No TradingAccount or signing credential is required to classify this initial Hyperliquid epoch as `NEVER_ACTIVATED` because their absence is part of the proof.

## Error handling

Use fail-closed 409-class application errors for safe-switch rejection with machine-readable blocker codes suitable for the UI.

Suggested categories:

- `pause_required`;
- `positions_not_flat`;
- `open_orders_present`;
- `pending_jobs`;
- `unresolved_executions`;
- `destination_unreadable`;
- `destination_identity_mismatch`;
- `provider_switch_unsupported` only where the target itself is unsupported, never as a substitute for unreadable source state.

Do not include credentials, signatures, private keys or secret-bearing provider responses in errors/logs.

## Concurrency and TOCTOU requirements

The design must preserve the same check-then-use discipline already applied to signed RISEx execution.

Required properties:

- switch checks are bound to the exact source provider/network/epoch;
- the source identity is revalidated immediately before epoch mutation;
- DB-local blockers are checked again after provider reads;
- an old verification result cannot authorize a different source epoch;
- two concurrent switch requests for the same user serialize through the user-row lock;
- an old job that races with the switch remains bound to the old epoch and is rejected by existing fences;
- provider-read failure at any point blocks the switch.

A manual external trade can never be made impossible purely by application locking. The provider verification must therefore be the last external observation before the local transition, with no unrelated network waits inserted after it.

## Scope of the implementation PR

Expected in scope:

- central destination-switch guard in `set_user_destination()`;
- shared DB-local switch-precheck helper;
- three-state source classification;
- Hyperliquid live position + open/conditional-order verification;
- fail-closed RISEx activated-source handling until complete private reads exist;
- `PUT /trading-provider` owner-scoped endpoint and schema;
- `/me`/dashboard provider + switch-readiness serialization;
- provider selector in Settings;
- reordering of current network-switch cleanup so evidence is not deleted before verification;
- regression tests for existing Hyperliquid defaults, network switching and epoch fences;
- TDD tests for all new provider-selection and safe-switch cases.

Explicitly out of scope:

- routing ordinary `execution-worker` jobs to RISEx;
- changing queue/strategy writer provider restrictions;
- sending a RISEx order;
- registering/revoking a RISEx signer;
- persisting a new user-specific RISEx credential model;
- RISEx mainnet enablement;
- `ENABLE_LIVE_TRADING`, database `live_trading`, production/mainnet gates or Railway topology changes;
- weakening any signed-testnet readiness, nonce, replay or freshness control.

## Required TDD acceptance cases

The implementation plan must include RED tests before production changes for at least these cases.

### Defaults and ownership

- new SIWE user still starts with provider `hyperliquid`;
- provider request requires an explicit provider value;
- provider endpoint operates only on `current_user` and exposes no arbitrary `user_id` target;
- same-provider request is idempotent and does not create a new epoch;
- master-source user cannot use follower provider controls.

### `NEVER_ACTIVATED`

- paused user with no TradingAccount, no credential and no epoch account address may switch Hyperliquid -> RISEx;
- the old epoch is closed and a distinct new epoch is created;
- old epoch fields remain unchanged;
- target network is preserved;
- SHADOW or ACTIVE state blocks the same switch until PAUSED;
- existence of any historical epoch with non-null `account_address` prevents `NEVER_ACTIVATED` classification;
- an inconsistent/missing source identity after activation history becomes `UNREADABLE`.

### `VERIFIED_FLAT`

- provider position read succeeds with all sizes zero and provider order read returns no regular/conditional orders -> switch allowed if all DB-local blockers are clear;
- any non-zero provider position blocks;
- any regular open order blocks;
- any conditional/trigger order blocks;
- provider position read failure blocks;
- provider order read failure/malformed payload blocks;
- local non-zero managed ledger blocks even if provider reports flat;
- no failed read is converted to flat.

### DB-local blockers

- not PAUSED blocks;
- pending `QUEUED` job blocks;
- pending `PROCESSING` job blocks;
- pending `RETRYING` job blocks;
- unresolved `SUBMITTING` execution blocks;
- unresolved `UNKNOWN` execution blocks;
- blockers are rechecked after provider verification before mutation.

### Central-boundary protection

- direct `set_user_destination()` provider/network switch without satisfying the guard fails;
- calling `close_user_destination_epoch()` first does not turn a later provider change into an unguarded bootstrap;
- concurrent switch attempts cannot both create competing active epochs;
- source epoch mismatch after verification blocks;
- network-switch API now relies on the same central invariant and cannot bypass provider-side flat/order verification.

### Provider-specific behavior

- active Hyperliquid source can become `VERIFIED_FLAT` only after live position and frontend/open-order reads both succeed;
- active RISEx source without complete account/order reads is `UNREADABLE` and switching is blocked;
- never-activated RISEx source can still switch because that state is DB-proven rather than provider-read-derived.

### Non-regression

- Hyperliquid remains the schema/application default;
- provider and network remain independent dimensions;
- historical job/execution destination identity is unchanged;
- stale-epoch job rejection remains intact;
- no automatic provider fallback is introduced;
- no signed RISEx write is reachable from provider selection;
- existing signed-testnet gates, replay protection and live pre-POST freshness remain unchanged.

## Design alternatives considered

### Endpoint-only guard

Rejected. It reproduces the current weakness: a future caller can call `set_user_destination()` without the endpoint precheck.

### Caller-supplied `verified_flat=True` / evidence object

Rejected as the authorization boundary. A caller could accidentally construct or reuse evidence that does not belong to the current epoch. Provider verification must be owned by the central transition path and bound to the source identity.

### Treat missing account as flat

Rejected. It collapses `NEVER_ACTIVATED` and `UNREADABLE` and recreates the exact ambiguity this design is intended to remove.

### Defer provider-side flat/open-order verification to a later PR

Rejected for Hyperliquid source switching. Exposing provider selection while retaining only local-ledger checks would weaken the approved 2026-09-09 architecture.

For RISEx, the semantics are not deferred: lack of complete reads maps to `UNREADABLE` and blocks. A later RISEx read-capability PR may turn that blocked state into `VERIFIED_FLAT` by adding evidence; it may not relax the gate.

## Acceptance criteria

This design is satisfied only when:

- provider selection is owner-scoped and explicit;
- Hyperliquid remains the default;
- provider/network switches cannot bypass `set_user_destination()` safeguards;
- `VERIFIED_FLAT`, `NEVER_ACTIVATED` and `UNREADABLE` are distinct and observable states;
- only `VERIFIED_FLAT` and `NEVER_ACTIVATED` can authorize a switch;
- provider read errors always block;
- Hyperliquid active-source switching verifies real positions and regular/conditional orders;
- activated RISEx source remains fail-closed until complete read evidence exists;
- every successful provider/network change creates a new epoch;
- old epoch/history identity is immutable;
- the empty `api Copy` database can progress from wallet login to a legitimate RISEx/testnet epoch without direct SQL or test bypasses;
- no execution routing or provider write is added by this PR.
