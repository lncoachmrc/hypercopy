# TRAXION RISEx Provider Selection and Safe-Switch Design

**Date:** 2026-09-13  
**Status:** approved for implementation planning  
**Baseline:** `main` at `0db51ba44d4fc2307b62a1069291c59a31d7acd5`  
**Parent architecture:** `docs/superpowers/specs/2026-09-09-risex-follower-integration-design.md`

## Goal

Complete the missing Phase 2 provider-selection path so an authenticated follower can explicitly choose `hyperliquid` or `risex` while preserving the immutable execution-epoch model and the original safe-switch semantics.

This work is not an execution-routing change. It creates and protects the destination identity that later execution code consumes. Hyperliquid remains the default provider. Selecting RISEx does not auto-enable trading, does not auto-create a RISEx credential, does not bypass signed-write readiness, and does not route ordinary worker traffic to RISEx.

The immediate isolated-test path that must become possible is:

1. wallet SIWE login on an empty database;
2. new user is created with the existing default `hyperliquid` provider and configured follower network;
3. the user explicitly pauses the strategy;
4. the owner-scoped provider endpoint selects `risex`;
5. the old initial Hyperliquid epoch is closed;
6. a new RISEx epoch is created on the same network;
7. no direct SQL, mock, monkeypatch or implicit provider default is involved.

## Existing architecture that must be preserved

The repository already provides:

- `users.execution_provider` with default/backfill `hyperliquid`;
- `users.execution_network` as an independent dimension;
- immutable `execution_epochs`;
- `set_user_destination()` as the function that closes an existing epoch and opens a new one;
- immutable job/execution destination binding;
- stale-epoch rejection at execution time;
- `PUT /trading-network` with endpoint-local switch blockers;
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

## Current network-switch risk classification

The existing `PUT /trading-network` path is already a production-facing Hyperliquid path. Its current `_network_switch_status()` gate checks local pause state, managed `PositionLedger`, pending jobs and unresolved executions, but it does not prove provider-side flatness and it does not inspect provider open/conditional orders.

Two separate issues must be distinguished:

1. **Cleanup ordering.** The endpoint currently deletes `TradingAccount`, local ledger and `RiskState` before calling `set_user_network()`. These operations occur in the same database transaction, so this ordering is not by itself treated as an independently exploitable committed-data-loss bug under the current implementation. If the later database transition fails before commit, the transaction is expected to roll back.
2. **Authorization weakness.** The endpoint can currently authorize a network switch based only on local state. If local ledger state diverges from the provider, or if provider-side open/conditional orders exist while local blockers are clear, the safe-switch semantics are weaker than the approved architecture. This weakness exists today; it is not created by provider selection.

Moving the authoritative guard into `set_user_destination()` necessarily changes the existing Hyperliquid network-switch path because `set_user_network()` delegates to `set_user_destination()`. For that reason, the existing path must be hardened in a dedicated backend PR with explicit Hyperliquid non-regression tests before provider selection is added.

## Decision 1 — the authoritative guard lives inside `set_user_destination()`

### Problem

Today `_network_switch_status()` protects `PUT /trading-network`, but `set_user_destination()` itself can close the current epoch and open another without checking pause, positions, pending jobs, unresolved executions or provider-side flat/order state. A future caller can therefore omit the endpoint-level precheck and still perform the state transition.

Provider selection would make that omission reachable from an additional application path.

### Decision

`set_user_destination()` becomes the authoritative destination-transition boundary.

Whenever the target `provider` or `network` differs from the user's persisted current destination identity, `set_user_destination()` MUST invoke the safe-switch authorization itself before closing or replacing any epoch.

The guard is not satisfied by a caller-provided boolean such as `safe=True`, and callers do not supply a forgeable `VERIFIED_FLAT` object. The central transition path owns classification and provider verification.

The function MUST distinguish these cases:

1. **Initial materialization** — no previous operational destination has ever existed. Existing Hyperliquid default/bootstrap remains valid without a switch authorization.
2. **No-op** — target provider/network equals the current destination and no destination identity change is requested. Return the current state without creating a new epoch.
3. **Provider/network switch** — target provider or network differs. Full safe-switch semantics apply.
4. **Same-provider credential/account rebind** — provider/network are unchanged but account/credential identity changes. This remains a separate lifecycle operation and MUST NOT accidentally inherit provider/network-switch semantics. Existing Hyperliquid account-link safety must remain covered by dedicated regression tests.

### Close-before-switch bypass resistance

The guard MUST NOT depend only on `active_execution_epoch_id` being non-null.

If another application path closes the active epoch first, `users.execution_provider` and `users.execution_network` still identify the previous persisted destination. `set_user_destination()` MUST compare the target to those persisted values and inspect prior epoch history. A caller cannot turn a provider/network change into apparent bootstrap by calling `close_user_destination_epoch()` first.

An unguarded creation is allowed only for genuine first-time materialization, not for a user with prior destination history whose target provider/network differs from the persisted identity.

### Transaction and freshness ordering

A provider/network switch is rare, so correctness takes priority over minimizing the duration of one user's transition transaction.

The central transition MUST:

1. lock/read the user destination identity;
2. evaluate DB-local blockers;
3. classify `NEVER_ACTIVATED` or perform provider-side verification;
4. re-check DB-local blockers after provider reads and before mutation;
5. confirm source epoch/provider/network identity still matches what was verified;
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

The expected empty-database path is therefore:

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

## Decision 3 — close the safe-switch gap before exposing provider selection

Provider selection must not be released while the existing network switch still uses weaker authorization semantics.

This is implemented in two backend PRs, in order.

### PR A — existing Hyperliquid network-switch hardening

Before adding any provider-selection endpoint, harden the existing destination-transition primitive and existing `PUT /trading-network` path.

PR A MUST include:

- central provider/network-switch authorization invoked by `set_user_destination()`;
- shared DB-local blocker evaluation;
- three-state source classification;
- live Hyperliquid position verification;
- live Hyperliquid open-order verification covering regular and trigger/conditional orders;
- reordering of `PUT /trading-network` so previous destination evidence is not destroyed before authorization;
- user-row/source-epoch TOCTOU protection;
- non-regression tests for existing Hyperliquid account linking, same-provider credential rebind, network switching, epoch fencing and rollback behavior.

PR A MUST NOT add the RISEx provider endpoint or frontend provider selector.

For Hyperliquid order verification, prefer the SDK/API `frontendOpenOrders` semantics because its response distinguishes trigger/TP/SL orders. Any read failure is `UNREADABLE`.

### RISEx source behavior in the shared guard

The current repository does not expose a complete authenticated/private RISEx account-state plus open/conditional-order read path suitable for proving an activated RISEx account flat.

The shared guard therefore treats:

- a never-activated RISEx destination as eligible for `NEVER_ACTIVATED`;
- an activated RISEx destination without complete position + order verification as `UNREADABLE`;
- any future complete RISEx read implementation as a separate follow-up capable of producing `VERIFIED_FLAT`, never as a reason to relax the guard.

### PR B — provider-selection backend

Only after PR A is merged and verified, add the explicit owner-scoped provider-selection API using the already-hardened destination transition boundary.

PR B MUST NOT change the authorization semantics introduced by PR A.

## Decision 4 — DB-local blockers live in the shared destination-transition layer

The current `_network_switch_status()` logic is useful for presentation but endpoint-local and incomplete.

A shared destination-switch precheck becomes the single source for DB-local blockers used by both provider and network changes.

At minimum it checks:

- `copy_state == PAUSED`;
- no non-zero managed `PositionLedger` row;
- no current-epoch `CopyJob` in `QUEUED`, `PROCESSING` or `RETRYING`;
- no current-epoch `Execution` in unresolved states (`SUBMITTING` or `UNKNOWN`; any later unresolved state added to the model must also be treated as blocking).

The mutation boundary inside `set_user_destination()` re-evaluates these conditions authoritatively. API readiness reporting may reuse the same helper for presentation but cannot substitute for the final check.

## Decision 5 — owner-scoped provider endpoint

PR B adds:

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

The previous destination must be authorized before destructive local cleanup.

The current network-switch endpoint deletes the `TradingAccount` and local ledger/risk state before calling `set_user_network()`. Because the hardening changes an existing production-facing Hyperliquid path, this reordering belongs exclusively to PR A, not to the provider-selection PR.

PR A ordering is:

- safe-switch verification first;
- epoch transition only after verification succeeds;
- provider-specific credential cleanup and local ledger/risk reset only after the transition decision, inside the same local transaction;
- any database failure before commit rolls back the local transition;
- no provider-side write is part of the switch.

Switching away from Hyperliquid removes the old Hyperliquid `TradingAccount`/`SigningCredential` only after safe-switch authorization succeeds. Returning to Hyperliquid requires linking a fresh dedicated API wallet through the existing Hyperliquid account-link flow.

## Backend readiness reporting

PR B should expose through `/me` / dashboard serialization at least:

- current `execution_provider`;
- current `execution_network`;
- local destination-switch readiness;
- structured local blockers.

Local readiness is not provider-verified flatness. Provider-side flat/order verification happens on the actual switch request because it must be fresh and because continuous provider polling would create unnecessary rate-limit load and stale authorization evidence.

## UI selection — required Phase 2 follow-up, not part of the first backend PRs

Phase 2 still requires a visible `Hyperliquid / RISEx` selector independent from `TESTNET / MAINNET`, but frontend work is deliberately deferred until the backend contract is merged and testable independently.

The UI is a separate PR after PR B because:

- the first RISEx test order can be reached through the API without UI;
- the deployed frontend uses a same-origin proxy and is not currently the authoritative end-to-end test surface for isolated `api Copy`;
- backend and frontend changes remain easier to review independently.

The later UI PR must preserve these rules:

- Hyperliquid remains selected by default for new users;
- a different provider cannot be selected while local blockers exist;
- the switch action tells the user that live destination verification is performed at confirmation time;
- a provider-read failure is shown as blocked, never as flat;
- selecting RISEx does not present it as trading-ready unless separate RISEx readiness/signer gates pass;
- existing Hyperliquid API-wallet fields must not be mislabeled as RISEx credentials.

The UI requirement remains part of the Phase 2 architecture; it is deferred in implementation sequence, not removed.

## Empty-database SIWE path

The isolated `api Copy` service is configured so wallet SIWE can create the first user on its empty database.

After PR A + PR B the backend flow is:

1. `POST /api/v1/auth/challenge`;
2. wallet signs the SIWE message;
3. `POST /api/v1/auth/verify`;
4. backend creates `User`, risk rows, trial subscription and initial destination epoch;
5. initial provider remains `hyperliquid` by schema/application default;
6. configured follower network is used (`testnet` on `api Copy`);
7. user explicitly pauses;
8. `PUT /api/v1/trading-provider {"provider":"risex"}`;
9. `set_user_destination()` proves `NEVER_ACTIVATED` and creates a new RISEx/testnet epoch.

No TradingAccount or signing credential is required to classify this initial Hyperliquid epoch as `NEVER_ACTIVATED` because their absence is part of the proof.

## Error handling

Use fail-closed 409-class application errors for safe-switch rejection with machine-readable blocker codes suitable for later UI use.

Required categories include:

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

The design preserves the same check-then-use discipline already applied to signed RISEx execution.

Required properties:

- switch checks are bound to the exact source provider/network/epoch;
- source identity is revalidated immediately before epoch mutation;
- DB-local blockers are checked again after provider reads;
- an old verification result cannot authorize a different source epoch;
- two concurrent switch requests for the same user serialize through the user-row lock;
- an old job racing with the switch remains bound to the old epoch and is rejected by existing fences;
- provider-read failure at any point blocks the switch.

A manual external trade cannot be made impossible purely by application locking. Provider verification therefore remains the last external observation before the local transition, with no unrelated network waits inserted afterward.

## Implementation sequence and PR boundaries

### PR A — Hyperliquid destination/network safe-switch hardening

Backend only.

In scope:

- `set_user_destination()` authoritative provider/network switch guard;
- shared DB-local switch blockers;
- `VERIFIED_FLAT / NEVER_ACTIVATED / UNREADABLE` classification;
- Hyperliquid live positions + regular/conditional open-order verification;
- fail-closed activated RISEx source classification where complete reads do not exist;
- `PUT /trading-network` cleanup reordering;
- Hyperliquid same-provider account/credential rebind regression coverage;
- network-switch and epoch-fence non-regression tests.

Out of scope:

- `/trading-provider`;
- frontend changes;
- RISEx order routing or writes.

### PR B — RISEx provider-selection backend

Backend only, based on merged PR A.

In scope:

- `PUT /trading-provider` schema and owner-scoped endpoint;
- explicit provider validation;
- provider switch preserving current network;
- `/me`/dashboard `execution_provider` and local readiness/blocker reporting;
- empty-database `NEVER_ACTIVATED` Hyperliquid -> RISEx path;
- activated RISEx source remains fail-closed `UNREADABLE`;
- backend API/integration tests.

Out of scope:

- frontend selector;
- execution-worker RISEx routing;
- new RISEx credential persistence;
- any provider write.

### PR C — provider selector UI

Tracked Phase 2 follow-up after backend verification. It consumes the merged PR B contract and contains frontend/UI tests only plus any narrowly required API client typing.

## Required TDD acceptance cases

### PR A RED cases — existing Hyperliquid path

- `PAUSED` + provider-side zero positions + zero regular/conditional orders + clean DB blockers allows Hyperliquid network switch;
- provider non-zero position blocks;
- regular open order blocks;
- conditional/trigger order blocks;
- provider position read failure blocks as `UNREADABLE`;
- provider order read failure/malformed payload blocks as `UNREADABLE`;
- local non-zero managed ledger blocks even if provider reports flat;
- SHADOW and ACTIVE block;
- `QUEUED`, `PROCESSING` and `RETRYING` jobs each block;
- `SUBMITTING` and `UNKNOWN` executions each block;
- blockers are rechecked after provider verification;
- source epoch/provider/network mismatch after provider verification blocks;
- direct `set_user_destination()` network switch cannot bypass the central guard;
- calling `close_user_destination_epoch()` first does not create an unguarded bootstrap;
- two concurrent switch attempts cannot produce competing active epochs;
- failed transition does not commit TradingAccount/ledger/risk cleanup;
- successful network switch closes old epoch, creates a distinct new epoch and preserves old history;
- existing Hyperliquid `POST /trading-account` same-provider credential/account rebind still functions and is not misclassified as a provider/network switch;
- stale-epoch job rejection remains intact;
- default provider remains Hyperliquid.

### PR B RED cases — provider-selection backend

- new SIWE user still starts with provider `hyperliquid`;
- provider request requires an explicit `provider` value;
- endpoint accepts only `hyperliquid | risex`;
- endpoint exposes no arbitrary `user_id` target and mutates only `current_user`;
- CSRF remains required;
- master-source user cannot use follower provider controls;
- same-provider request is idempotent and creates no new epoch;
- changing provider preserves current network;
- paused user with no TradingAccount, no credential and no epoch account address switches Hyperliquid -> RISEx via `NEVER_ACTIVATED`;
- old epoch is closed and a distinct RISEx epoch is created;
- old epoch fields remain unchanged;
- existence of any historical epoch with non-null `account_address` prevents `NEVER_ACTIVATED` classification;
- inconsistent/missing source identity after activation history is `UNREADABLE`;
- activated RISEx source without complete provider reads cannot switch;
- response serialization exposes provider and local switch blockers;
- provider selection never sends a RISEx write and never auto-resumes trading;
- queue/worker Hyperliquid-only restrictions remain unchanged.

### Non-regression across both backend PRs

- Hyperliquid remains the schema/application default;
- provider and network remain independent dimensions;
- historical job/execution destination identity remains immutable;
- no automatic provider fallback is introduced;
- no signed RISEx write is reachable from provider selection;
- existing signed-testnet gates, replay protection and live pre-POST freshness remain unchanged;
- `writes_enabled` and other signed-write compile-time/runtime fences remain unchanged unless separately authorized.

## Design alternatives considered

### Endpoint-only guard

Rejected. It reproduces the current weakness: a future caller can call `set_user_destination()` without endpoint prechecks.

### Caller-supplied `verified_flat=True` or externally constructed authorization evidence

Rejected as the authorization boundary. A caller could accidentally construct/reuse evidence for the wrong epoch. Provider verification is owned by the central transition path and bound to source identity.

### Treat missing account as flat

Rejected. It collapses `NEVER_ACTIVATED` and `UNREADABLE` and recreates the ambiguity this design removes.

### Defer Hyperliquid provider-side flat/open-order verification until after provider selection

Rejected. The weakness already exists in the network-switch path and provider selection must not widen the number of callers before the invariant is centralized.

### Mix network-switch hardening and provider endpoint in one PR

Rejected. The hardening changes an existing production-facing Hyperliquid path. It requires dedicated regression review and must be independently reversible before the new provider-selection API is introduced.

### Mix backend and frontend provider selection

Rejected. Backend behavior is sufficient for isolated API testing and the same-origin production frontend is a separate integration surface. UI remains a required follow-up PR.

## Acceptance criteria

The design is satisfied only when:

- PR A independently hardens the existing Hyperliquid network-switch path before provider selection exists;
- PR B independently adds owner-scoped explicit provider selection on top of the hardened boundary;
- Hyperliquid remains the default;
- provider/network switches cannot bypass `set_user_destination()` safeguards;
- `VERIFIED_FLAT`, `NEVER_ACTIVATED` and `UNREADABLE` remain distinct;
- only `VERIFIED_FLAT` and `NEVER_ACTIVATED` can authorize a switch;
- provider read errors always block;
- Hyperliquid active-source switching verifies real positions and regular/conditional orders;
- activated RISEx source remains fail-closed until complete read evidence exists;
- every successful provider/network change creates a new epoch;
- old epoch/history identity remains immutable;
- the empty `api Copy` database can progress from wallet login to a legitimate RISEx/testnet epoch without direct SQL or test bypasses;
- UI provider selection remains tracked for PR C but is absent from PR A and PR B;
- no execution routing or provider write is added by these backend PRs.
