# ADR-0004 — RISEx continuous execution authorization

- **Status:** Accepted
- **Date:** 2026-09-13
- **Scope:** TRAXION / RISEx continuous execution-worker authorization lifecycle
- **Decision owner:** TRAXION project owner
- **Related decisions:** ADR-0002 — RISEx session-key authorization model; ADR-0003 — RISEx fund-movement negative-probe applicability
- **Mandatory review date:** 2026-12-13

## Context

`RISExRuntimeReadinessAttestation` is deliberately process-local, sealed and short-lived. It is emitted only by `run_signed_testnet_readiness()` after an explicitly armed readiness run passes, and its current TTL is 300 seconds.

That TTL is useful for the manual/test readiness flow because it prevents a PASS gathered in one operational context from being reused much later as if it were still evidence for an immediate action.

The execution worker has a different lifecycle. It is intended to run continuously, and there is no operator who should have to re-arm it every five minutes.

Two superficially simple solutions are rejected:

- automatic readiness refresh in the worker, because a self-renewing PASS would turn the 300-second TTL into a decorative check and recreate a check-then-use authorization pattern;
- a very long or infinite readiness TTL, because that would convert a bounded readiness observation into persistent authorization.

The runtime therefore needs a distinction between:

1. **readiness evidence** — a recent observation that the current RISEx runtime context passed the signed readiness checks;
2. **arming/disarming request** — a durable, one-shot operator command asking one specific live worker process incarnation to attempt an authorization transition; and
3. **continuous execution authorization** — explicit operator intent, materialized only in that worker's process memory, that the specific process may continue attempting RISEx execution while its security-relevant context remains unchanged and all per-operation controls continue to pass.

ADR-0004 defines the third concept as a process-local **RISEx Operational Execution Window**.

The durable arming request is deliberately not the authorization itself. A persisted request can cause one readiness attempt. It can never be read as proof that readiness passed or that a window is open.

## Deployment verification that constrains the design

The current Railway deployment does not provide an executable shell/exec control path for the running `execution-worker` that can be used as the normal operational authorization mechanism. `railway run` executes a command locally with Railway variables rather than inside the deployed worker process.

Therefore the earlier `SIGUSR1` / `SIGUSR2` design is removed: process signals are not an executable operator control mechanism on this deployment.

The deployed `execution-worker` is currently configured as one Railway replica. The application is nevertheless replica-aware through `replica_identity()` / `RAILWAY_REPLICA_ID` and `WorkerHeartbeat`, so ADR-0004 must define behavior if the service is later scaled or if more than one live replica is observed.

The repository already has PostgreSQL `system_flags`, but that table is structurally level-triggered (`slug`, `enabled`, `value`). Encoding one-shot request identity, target process incarnation, consumption state and monotonic command generation inside its JSON payload would simulate an event model on top of a level-triggered flag. The accepted implementation therefore uses a dedicated additive table, `risex_execution_control`, whose columns directly express the one-shot control model. Readiness PASS and the Operational Execution Window remain non-persistent.

## What the 300-second readiness TTL protects

The 300-second TTL remains meaningful at the boundary where readiness evidence is converted into an execution authorization decision.

It prevents an old readiness result from being used to open a new operational window after the context in which the evidence was collected may have become stale.

For a continuously running worker, however, the passage from second 299 to second 301 is not itself a security-relevant context transition. If the process, deployment identity, provider, network, account, signer, permissions and security-relevant configuration are unchanged, the worker is still in the same operational epoch.

Accordingly, the readiness TTL must **not** be interpreted as the lifetime of the continuous worker authorization.

The TTL protects the **bootstrap from readiness to an operational window**. It does not define the duration of that window.

## What the readiness attestation does not protect

The readiness attestation is not responsible for every runtime security property.

The following independent controls remain binding and unchanged:

- gate 1;
- gate 2;
- gate 4;
- the mandatory freshness probe immediately before each provider POST.

The freshness probe remains the control that detects current provider-side authorization/revocation state at the point of use.

Without a separate continuous-execution authorization mechanism, the system would still lack protection against:

- a worker process starting RISEx execution without a new explicit operator authorization;
- a process restart silently inheriting the effect of a previous readiness PASS;
- a security-relevant configuration, signer, account, network or deployment change being treated as part of the previous authorization epoch;
- a previously issued readiness attestation being reused to open multiple operational epochs;
- continuous execution persisting indefinitely without periodic human reaffirmation of intent;
- multiple worker replicas having inconsistent process-local authorization state and therefore intermittently accepting or rejecting equivalent RISEx work;
- a DISARM or other invalidation arriving while an ARM readiness attempt is in flight and being lost before the eventual PASS is converted into a window.

ADR-0004 addresses those gaps without replacing gate 1, gate 2, gate 4 or the mandatory freshness probe. Gate 3 is specialized below because its manual/test and continuous-worker authorization semantics are intentionally different.

## Decision

### 1. Separate readiness evidence, operator request and continuous execution authorization

`RISExRuntimeReadinessAttestation` remains a short-lived readiness proof with a 300-second TTL.

A valid readiness attestation may be consumed once to open a process-local `RISEx Operational Execution Window`.

The three security concepts have intentionally different persistence semantics:

| Concept | May be persisted? | Meaning |
| --- | --- | --- |
| Arming/disarming request | Yes, as one-shot control-plane state | An operator asked a specific live worker incarnation to attempt ARM or DISARM |
| Readiness PASS / `RISExRuntimeReadinessAttestation` | No | Recent process-local evidence that signed readiness passed |
| Operational Execution Window | No | Current process-local authorization for continuous RISEx execution |

Persisting the request does not violate the no-persistence rule for PASS/window because the request is neither evidence of readiness nor an authorization capability.

The operational window never substitutes for gate 1, gate 2, gate 4 or the immediate pre-POST freshness probe.

### 2. The readiness attestation is one-shot for window creation

A readiness attestation used to open an operational window is consumed atomically and cannot be reused to open another window.

Closing, expiring or invalidating a window does not make its original readiness attestation reusable, even if the 300-second readiness TTL has not yet elapsed.

Opening a later window requires:

1. a new explicit ARM request from an authorized operator;
2. atomic one-shot consumption of that request by its intended worker incarnation;
3. a new signed readiness run;
4. a new readiness PASS and attestation;
5. a final arm-attempt validity fence as defined below;
6. one-shot consumption of that new attestation into the new process-local window.

No automatic or background rearming is permitted.

### 2A. Gate 3 has two explicit, mutually exclusive authorization modes

The shared RISEx order adapter may serve both the short-lived manual/test path and the ADR-0004 continuous execution-worker path, but **each adapter instance must be constructed in exactly one explicit gate-3 authorization mode**. The implementation may choose the concrete type or enum name, but the semantic choice must be explicit at construction time and immutable for the lifetime of that adapter instance.

The two accepted modes are:

1. **Short-lived attestation mode** — used by the existing manual/test signed-execution path. Gate 3 is satisfied only by a currently valid `RISExRuntimeReadinessAttestation` whose account and signer identities match the specific prepared order request. The 300-second TTL remains fully enforced for this mode.
2. **Continuous operational-window mode** — used only by the continuous `execution-worker` governed by ADR-0004. The readiness attestation is consumed once during ARM/finalization to create the process-local Operational Execution Window. For subsequent orders in that same authorized process/context epoch, gate 3 is satisfied by the current process-local continuous authorization only after the worker re-checks the window and all relevant final local authorization conditions at the point of use.

These modes are mutually exclusive. The adapter must never infer the mode from which fields happen to be present. In particular, the following behavior is forbidden:

> if an attestation is present, use it; otherwise fall back to an Operational Execution Window.

That would make an incorrectly constructed adapter silently select the more permissive continuous path.

Therefore:

- construction without an explicit gate-3 mode is fail-closed;
- construction or invocation that supplies incompatible authorization material for both modes is fail-closed;
- a short-lived-mode adapter may not accept an Operational Execution Window as a substitute for an absent or expired attestation;
- a continuous-mode adapter may not use a leftover readiness attestation as a substitute for a non-authorized window;
- there is no automatic fallback or runtime switching from one mode to the other;
- absence, ambiguity or mismatch of the selected mode's required authorization material blocks the order before gate 4 and before any provider POST.

For the continuous mode, an `ENABLED` Operational Execution Window is the gate-3 continuous authorization after the one-shot readiness bootstrap. `DISABLED`, `ARMING`, `PAUSED`, `LOCKED`, an expired window, a restarted process, a context mismatch or a failed final singleton/local-authorization condition does not satisfy gate 3.

This specialization does **not** change gate 1, gate 2, gate 4 or the mandatory freshness probe immediately before provider POST. It also does not weaken the ADR-0004 point-of-use window check, singleton requirement, context/invalidation fences or the 24-hour maximum.

The continuous path must not periodically refresh the original readiness attestation and must not extend its 300-second TTL. A new Operational Execution Window always requires a new explicit ARM request, a new readiness run, a new PASS/attestation and the complete finalization fence.

### 3. Every worker process gets a fresh process-local `boot_id`

At each `execution-worker` process start, the worker must generate a cryptographically random `boot_id` in memory.

The `boot_id` identifies that exact process incarnation and is distinct from the stable-per-replica `worker_id` / `RAILWAY_REPLICA_ID`.

The worker publishes the `boot_id` only as telemetry in its own `WorkerHeartbeat.meta`.

The `boot_id` must never be loaded from:

- an environment variable;
- the `risex_execution_control` table;
- Redis;
- a database row;
- a file;
- a previous heartbeat.

It must never be reconstructed after restart.

The worker must never read `WorkerHeartbeat.meta.boot_id` as authorization input. Its authoritative boot identity is the random value held in its own process memory.

Therefore:

- restart creates a new `boot_id`;
- any request targeted to the previous `boot_id` becomes unusable by the new process;
- restart cannot silently replay an outstanding ARM request intended for the old process incarnation.

### 4. Concrete arming/disarming mechanism: dedicated one-shot control table

The selected control channel is the dedicated PostgreSQL table:

`risex_execution_control`

The table is a **command log/mailbox**, not an authorization table. Each command is a separate row containing at least:

```text
request_id: UUID PRIMARY KEY
control_generation: integer
action: ARM | DISARM
target_worker_id: string
target_boot_id: UUID
state: REQUESTED | CONSUMED
requested_at: timestamp
requested_by: UUID | null
reason: text
consumed_at: timestamp | null
consumed_by_worker_id: string | null
consumed_by_boot_id: UUID | null
supersedes_request_id: UUID | null
```

Implementation-specific request metadata needed to perform the one-shot readiness attempt may also be stored on the request row, but no readiness PASS, readiness attestation or Operational Execution Window may be stored there.

A new operator command must use a new random `request_id` and a strictly increasing `control_generation` for the same `target_worker_id` + `target_boot_id` incarnation.

The design moved from `system_flags` to this dedicated table because `system_flags` is natively level-triggered. Direct columns for request identity, target boot, state and generation express the accepted one-shot model without treating a JSON value as a synthetic event log.

Generation allocation and command creation must be serialized by the same transaction-scoped PostgreSQL advisory lock used by ARM finalization, keyed to the target `worker_id` + `boot_id`. Two concurrent operator commands for the same process incarnation must never receive the same generation.

A normal new ARM command must not silently overwrite another unconsumed `REQUESTED` command. A DISARM is different: it is a higher-priority cancellation command and **must be able to supersede a pending or already-consumed ARM request**, with a new generation and explicit audit correlation to the superseded request. A DISARM must never be rejected merely because an ARM readiness attempt is in flight.

### 5. Operator API for ARM and DISARM

The implementation must expose a CSRF-protected SUPERADMIN write endpoint dedicated to this control action:

`POST /admin/risex-execution-control`

The request must explicitly contain:

- `action` = `ARM` or `DISARM`;
- `target_worker_id`;
- `target_boot_id`;
- operator confirmation text appropriate to the action;
- `reason`.

The endpoint must validate before creating an ARM request that:

- the target heartbeat is currently fresh;
- the supplied `target_worker_id` and `target_boot_id` match the currently reported live process incarnation;
- exactly one live `execution-worker` replica exists under the singleton rule below;
- there is no existing unconsumed non-supersedable `REQUESTED` control command.

For DISARM, the endpoint must still validate the target identity when it is observable, but cancellation is fail-closed and has priority over an ARM attempt. If a targeted ARM is `REQUESTED`, `CONSUMED`, or reported as `ARMING`, the DISARM must advance the control generation and supersede/cancel that attempt rather than being blocked by it.

The endpoint creates a new `request_id`, atomically advances `control_generation`, writes the one-shot `REQUESTED` command and records the operator audit event.

The endpoint does **not** run readiness, open a window or claim success merely because the request was persisted.

Its successful response means only:

> the explicit operator request was accepted into the control channel for the targeted live process incarnation.

### 6. Request consumption must be atomic and one-shot

A worker must never implement command consumption as an unlocked `SELECT` followed later by an independent `UPDATE`.

Consumption must be one indivisible database state transition so that two processes cannot consume the same `request_id`.

The implementation must use one of these equivalent atomic patterns:

1. `SELECT ... FOR UPDATE` on the selected `risex_execution_control` request row inside one database transaction, verify all claim predicates while holding the row lock, mutate `REQUESTED -> CONSUMED`, then commit; or
2. a single conditional `UPDATE ... WHERE state = 'REQUESTED' AND request_id = ... AND control_generation = ... AND target_worker_id = ... AND target_boot_id = ...` and require exactly one affected row.

The claim predicates must include at least:

- `state == REQUESTED`;
- expected `request_id`;
- expected `control_generation`;
- `target_worker_id == self.worker_id`;
- `target_boot_id == self.boot_id`.

The worker must persist `CONSUMED`, `consumed_at`, `consumed_by_worker_id` and `consumed_by_boot_id` **before** executing readiness or changing the process-local window.

The transaction must commit before any potentially slow provider/readiness network operation begins.

Therefore a crash after command consumption but before readiness completion cannot cause the same request to be retried automatically. Another attempt requires a new explicit operator request.

This is intentional fail-closed behavior.

### 7. ARM request semantics and observable `ARMING` state

After atomically consuming an ARM request, the targeted worker performs exactly one authorization attempt.

The worker must create an explicit process-local arm-attempt object containing at least:

```text
arm_request_id
arm_control_generation
arm_started_at
arm_context_fingerprint
arm_invalidation_epoch_snapshot
```

The worker must then transition its reported state to `ARMING` and publish the in-flight attempt through `WorkerHeartbeat.meta` before beginning the network-bound readiness evaluation.

`ARMING` is an observable non-authorized state. While `ARMING`:

- no RISEx provider POST is permitted by virtue of the arm attempt;
- `authorization_status` must not be `AUTHORIZED`;
- a targeted DISARM cancels the attempt as defined below;
- any event that would invalidate or LOCK an existing window must also invalidate the arm attempt.

The process maintains a monotonic, process-local `authorization_invalidation_epoch` for the current boot. Any security-relevant event that would destroy an existing window increments this epoch. The ARM attempt snapshots the epoch before readiness. This epoch is process-local state and must not be reconstructed from persisted telemetry.

Before readiness can eventually open a window, the worker must verify the singleton precondition and all required readiness prerequisites.

If readiness:

- fails;
- times out;
- raises;
- cannot establish the singleton precondition;
- cannot produce a valid attestation; or
- returns PASS but the final arm-attempt validity fence fails,

then the already-consumed ARM request remains consumed and **no window opens**.

A new attempt always requires a new operator ARM request with a new `request_id` and a later `control_generation`.

The worker must not poll a consumed ARM request to regenerate readiness from it.

### 8. DISARM request semantics

A DISARM request is targeted and consumed through the same atomic one-shot mechanism, but its cancellation effect is broader than closing an existing window.

A DISARM targeted to the worker's current `worker_id` + `boot_id` must:

1. cancel any in-flight `ARMING` attempt for that process incarnation;
2. invalidate/discard any readiness PASS or attestation produced by that canceled attempt before it can become a window;
3. close any already-open operational window;
4. increment the process-local `authorization_invalidation_epoch`; and
5. transition the process to `DISABLED` unless a stronger fail-closed state such as `LOCKED` already applies.

DISARM does not require readiness.

The cancellation requirement applies even when the worker has already atomically consumed the earlier ARM and the network-bound readiness call has not yet returned. There does not need to be an existing window for DISARM to have effect.

If the target process has already restarted, its previous window and arm attempt have already been destroyed by the restart boundary. The new process must not consume the old DISARM request because its `boot_id` differs.

### 8A. In-flight ARM cancellation and finalization fence

This section closes the race where an ARM is consumed, readiness runs for seconds, a DISARM arrives while readiness is in flight, and the eventual PASS would otherwise open a window after the operator requested cancellation.

Every control command has a monotonic `control_generation`. An ARM attempt captures the generation of the ARM request it consumed. **Any later control generation targeted to the same worker/boot invalidates that ARM attempt.** In particular, a later DISARM cancels the attempt whether or not the worker has already processed the DISARM into local state.

A DISARM that is successfully committed to `risex_execution_control` while readiness is in progress therefore cancels the in-flight attempt. If the worker's control loop observes it immediately, it marks the local attempt canceled and increments `authorization_invalidation_epoch`. If the worker does not observe it until readiness returns, the finalization fence below detects the newer generation and rejects the PASS.

After readiness returns PASS, but **before** the readiness attestation is consumed into an Operational Execution Window, the worker must perform a final fail-closed revalidation. Command creation and ARM finalization must both acquire the same transaction-scoped PostgreSQL advisory lock keyed by `worker_id` + `boot_id`. While that advisory lock is held, finalization must verify all of the following:

- the current `worker_id` and in-memory `boot_id` still match the arm attempt;
- the consumed ARM row still refers to the same ARM `request_id`;
- its `control_generation` is exactly the ARM attempt's captured generation;
- its action is still `ARM` and its state is `CONSUMED`;
- no row with a later control generation for the same target worker/boot has been committed;
- the process-local `authorization_invalidation_epoch` still equals the attempt's snapshot;
- the security-relevant runtime context fingerprint still matches the attempt's snapshot;
- the singleton execution-worker invariant still holds;
- the readiness attestation is still valid and unused.

`SELECT ... FOR UPDATE` on the consumed ARM row is **not sufficient** for this serialization boundary: a concurrent DISARM is a new `INSERT`, so it does not need the ARM row lock. The shared advisory lock is required specifically so a DISARM command creation and ARM finalization cannot pass each other through that gap.

The operator ARM/DISARM endpoint and the finalization check therefore serialize on the same advisory-lock namespace. This gives command creation and window finalization an explicit linearization order:

- if a DISARM acquires the advisory lock and commits first, its higher generation is observed and the ARM PASS is discarded; **no window opens**;
- if finalization acquires the advisory lock first after readiness has completed, it may open the window only after all checks pass; a DISARM serialized after that point is an ordinary post-open DISARM and must close the window through the normal cancellation path.

The worker must hold the advisory-lock transaction through the local transition from `ARMING` to `ENABLED`. If the serialization transaction/connection fails before the finalization boundary is safely completed, the result is fail-closed: the attestation is discarded and no window may remain open.

A final unlocked `SELECT` followed by later local window creation is forbidden. That would recreate the same check-then-use race.

Any mismatch, ambiguity, database error, inability to establish the singleton invariant, or evidence of a later invalidation causes the arm attempt to terminate without a window. In doubt, the window does not open.

### 9. RISEx requires an execution-worker singleton

RISEx continuous signed execution has an explicit operational precondition:

> exactly one live `execution-worker` replica may exist.

This is a real limitation of the current design because the Operational Execution Window is intentionally process-local. Supporting multiple replicas would require a separate distributed authorization design and is outside ADR-0004.

Railway configuration currently declares one replica, but configuration alone is insufficient. The worker must verify the singleton invariant at runtime from `WorkerHeartbeat` records.

For ADR-0004, an `execution-worker` heartbeat is **live** only when:

- `service == 'execution-worker'`; and
- `now - seen_at <= 180 seconds`.

The actual worker emits heartbeat updates from more than one path. While idle, the consume loop uses a 2000 ms Redis stream block and emits a heartbeat after an empty read, so a healthy idle worker normally updates approximately every two seconds. The maintenance loop separately emits a heartbeat after its work and then sleeps for the configured reconciliation interval, normally 60 seconds. Therefore the 180-second live/stale threshold is deliberately conservative: crossing it means both the fast consume-loop observations and the independent maintenance path have failed to update PostgreSQL, which is consistent with a stopped worker or a worker isolated from the database. It is an operational liveness threshold, not freshness of RISEx authorization.

The singleton invariant must be checked:

- before accepting an ARM request at the SUPERADMIN API;
- by the worker after atomically consuming ARM and before readiness begins;
- again at the final arm-attempt validity fence before a PASS can open a window;
- continuously while a window exists, no less often than the normal heartbeat/maintenance cycle; and
- at the final local authorization check before a RISEx execution attempt may proceed to the existing per-operation gates.

If more than one live `execution-worker` heartbeat is observed, RISEx execution is fail-closed, any in-flight ARM attempt is invalidated, and any existing window must transition to `LOCKED` and be invalidated.

If zero live worker heartbeats can be established by the administrative control/read model, the system must not claim an executable authorization state.

Scaling `execution-worker` above one replica therefore requires a new ADR or an explicit revision of ADR-0004 before RISEx signed continuous execution can remain enabled.

#### Control-channel polling latency

The worker checks the control channel in the existing consume loop before each Redis `XREADGROUP`, giving a normal idle ARM/DISARM observation latency of roughly the existing two-second stream block rather than waiting for the maintenance interval. The existing maintenance loop is also a periodic control/invariant path; no second scheduler is introduced.

There is a documented limit: while the consume loop is synchronously processing a long-running job, that consume-loop fast poll does not run. The concurrent maintenance task still provides periodic observation, but the design must not treat polling latency as the final safety boundary. Step 4B, which wires RISEx into job execution, must re-check the process-local window and the relevant final authorization conditions **at the point of use** before entering the existing RISEx per-operation gates/POST path. A prior successful poll is never sufficient authorization for a later RISEx POST.

### 10. Bind the window to the security-relevant runtime context

> **Multi-user implementation note:** the Accepted amendment below supersedes the account/signer fingerprint wording in this section for continuous multi-user execution (Implemented in PR #217).

An operational window is valid only for the process and context in which it was opened.

Its context identity must include at least:

- `worker_id`;
- process-local `boot_id`;
- application build/version identity;
- RISEx provider identity;
- environment;
- chain/network identity;
- account/owner identity;
- session-key public identity;
- relevant Authorization/Router or equivalent deployment identities;
- security-relevant runtime configuration;
- effective permission/security scope used by the readiness decision.

Naturally dynamic per-operation values such as balances, nonces or market state are not part of the window identity when they are already covered by per-operation validation.

Any change or mismatch in a security-relevant context component invalidates the current window and any in-flight ARM attempt.

### 10A. Continuous gate 3 preserves the per-order account/signer binding

> **Multi-user implementation note:** the Accepted amendment below supersedes the window-bound account/signer wording here with per-order binding for continuous multi-user execution (Implemented in PR #217).

The Operational Execution Window's context fingerprint includes the account/owner identity and the session-key public identity used at ARM/finalization. That fingerprint is necessary to bind the window to the runtime context, but **an opaque fingerprint checked only at window creation is not by itself equivalent to the per-order identity binding enforced by the short-lived attestation path**.

The existing short-lived gate 3 validates the readiness attestation against `request.permit.account_address` and `request.permit.signer_address` for the specific prepared order. Continuous mode must preserve an equivalent check on every order.

Therefore the continuous authorization context must retain, in process-local non-persistent form, the exact account identity and signer/session-key public identity authorized when the window was opened, or expose an equivalently immutable process-local representation from which those exact identities can be checked. Storing only a hash that cannot be compared to the request permit is insufficient.

For every continuous-mode `place_ioc` attempt, before gate 4 and before any provider POST, gate 3 must verify all of the following:

- the selected adapter mode is explicitly continuous operational-window mode;
- the current process-local window remains `ENABLED` after expiry and final local authorization checks;
- `request.permit.account_address` equals the account identity bound to that current window/authorization context;
- `request.permit.signer_address` equals the signer/session-key public identity bound to that current window/authorization context.

The expected account/signer identities used by this comparison must come from the same process-local authorization context that produced the window. They must not be reconstructed from `WorkerHeartbeat`, the administrative status read model, a database row, Redis, or another persisted telemetry source.

A permit identity mismatch fails gate 3 before gate 4 and before the provider POST. It is a security-negative mismatch and is handled under the existing fail-closed/LOCKED rules; it must not be downgraded to PAUSED, deferred as an availability condition, or repaired by switching authorization modes.

This preserves the security property introduced by PR #168: continuous execution changes the lifetime carrier of gate-3 authorization, but it does not weaken the binding between the authorized account/signer identity and the specific order request.

### 10B. Continuous submission is fenced again after freshness and immediately before POST

The pre-gate-4 window check in section 10A is necessary but not sufficient because gate 4 contains an awaited provider freshness probe. A window may expire, be explicitly DISARMED, or be invalidated by a concurrent process-local task while that network call is in flight. A positive freshness result collected after such an invalidation must never authorize a later provider POST.

Every continuous-mode order attempt must therefore create a process-local operation snapshot before entering the awaited freshness probe. The snapshot must bind at least:

- the current `authorization_invalidation_epoch`;
- the current window/context identity;
- the account identity bound to the window;
- the signer/session-key public identity bound to the window; and
- the permit account/signer identities for the specific request.

The freshness probe must run **without holding the local submission-serialization mutex**, so DISARM, expiry and other invalidation work can proceed while the network call is in flight.

Immediately after a positive freshness probe returns, and **immediately before the provider POST**, the continuous path must acquire a process-local submission-serialization mutex shared with every local transition that can invalidate continuous authorization. While holding that serialization boundary it must revalidate, fail closed, all of the following:

- `expire_if_needed()` has been applied and the window state is still exactly `ENABLED`;
- the window has not reached `expires_at`;
- the current `authorization_invalidation_epoch` is exactly equal to the operation snapshot;
- the security-relevant window/context identity still equals the operation snapshot;
- `request.permit.account_address` still equals the account identity bound to the current window;
- `request.permit.signer_address` still equals the signer/session-key public identity bound to the current window;
- any process-local cancellation marker for that operation is still clear; and
- all other final local authorization conditions that ADR-0004 requires at point of use still hold.

The final fence and provider submission must have one explicit process-local linearization order relative to DISARM/expiry/LOCK/context-invalidation transitions. No invalidating local transition may interleave between the successful post-freshness fence and the provider submission. A conservative implementation may hold the submission-serialization mutex through the provider POST attempt; an alternative implementation is acceptable only if it provides an equivalent, reviewable serialization boundary proving that the authorization cannot change between the final fence and submission.

The following pattern is explicitly forbidden:

> verify window -> `await` freshness probe -> provider POST

when there is no second local authorization fence after the awaited probe. This is the same class of check-then-use error as the unlocked final `SELECT` before window creation rejected in section 8A: a previously true authorization fact is not authority for a later mutation across an uncontrolled await boundary.

#### In-flight cancellation during freshness

Each continuous order attempt must have a process-local cancellation marker/token bound to the same authorization epoch. A DISARM, window expiry, LOCK transition, context invalidation or equivalent authorization-ending event must invalidate the epoch and mark matching in-flight operation tokens canceled as soon as that event is processed locally.

The implementation should abort/cancel an in-flight freshness probe when the underlying async operation is safely cancellable. Cancellation is a responsiveness mechanism, not the security proof: if the provider/network call cannot be interrupted promptly, the mandatory post-freshness fence still observes the changed epoch/state/token and blocks the POST.

The submission mutex must not be held while the freshness probe is awaited. Therefore a DISARM processed during the probe can invalidate the operation before the probe returns rather than being forced to wait behind the network call.

#### Classification when authorization changes during the probe

Fail-closed behavior is mandatory, but the resulting state follows the cause rather than treating every change as the same security event:

- **explicit DISARM or ordinary hard expiry** is deterministic loss of authorization, not evidence of compromise. No POST is allowed; the operation follows the existing non-authorized/window-unavailable deferral path without consuming the failure budget, and the window remains/ends `DISABLED` according to the existing state rules;
- **permit account/signer mismatch, security-relevant context mismatch, singleton violation, negative security evidence, or another trusted security-negative invalidation** requires `LOCKED`; no POST and no availability-style deferral is permitted;
- **an invalidation-epoch change whose cause cannot be established unambiguously** is treated as `LOCKED` under the existing "when in doubt, LOCKED" rule.

This classification preserves the existing ADR semantics that expiry and explicit DISARM return the process to `DISABLED`, while identity/context/security failures destroy authorization as `LOCKED`.

### 11. Maximum operational window: 24 hours

A successfully opened operational window has a hard maximum lifetime of **24 hours**.

The 24-hour limit is an **operator-intent horizon**, not a freshness guarantee.

The rationale is operational:

- the execution worker is a 24/7 service and must not assume an eight-hour staffed shift; an 8-hour ceiling would require up to three manual re-arms per day and would create strong pressure to automate the very control this ADR is designed to keep explicit;
- a 72-hour ceiling would allow one human authorization to survive across several daily operating cycles and multiple unattended periods, which weakens the purpose of bounded human intent;
- 24 hours requires a fresh human authorization at least once per day while avoiding a shift-specific assumption or a wall-clock/time-zone dependency.

A 24-hour window may cross a night if it was opened late in the day. This ADR does **not** claim otherwise. The security property is that no authorization survives for more than one 24-hour operational cycle without a fresh explicit arming act and fresh readiness PASS.

The window lifetime is never extended by:

- successful orders;
- worker activity;
- successful freshness probes;
- health checks;
- heartbeat publication;
- readiness refresh;
- a process restart.

There are zero automatic renewals.

When the 24-hour maximum is reached, RISEx execution becomes unauthorized until a new explicit ARM/readiness/window cycle completes.

### 12. PAUSED versus LOCKED is determined by evidence quality, not optimism

The worker uses the following fail-closed rule:

- **PAUSED** is permitted only when the worker cannot obtain a semantically trustworthy security verdict because of a pure reachability/availability failure;
- **LOCKED** is required when a received response is interpretable as security-negative;
- **LOCKED** is also required when a response is malformed, contradictory, incomplete or otherwise ambiguous for a security decision;
- **when in doubt, LOCKED**.

Examples that may be classified as PAUSED, provided no security-negative response was obtained:

- connection timeout;
- connection refused/reset;
- DNS/network reachability failure;
- transient transport interruption;
- an explicitly availability-only response such as a gateway/service-unavailable condition that carries no usable authorization verdict.

Examples that require LOCKED include:

- session key revoked or inactive;
- account/signer mismatch;
- missing required permission;
- deployment identity mismatch;
- multiple live execution-worker replicas;
- explicit provider rejection on the security dimension being checked;
- a syntactically received but malformed or semantically incomplete response where the required security state cannot be established.

TLS/authenticity failures are not treated as harmless reachability failures. If the worker cannot establish that it is communicating with the expected authenticated endpoint, the result is LOCKED.

### 13. PAUSED may auto-resume only because no negative security fact was observed

A PAUSED window remains the same operational window and does not create or renew authorization.

The worker may resume RISEx execution automatically from PAUSED only if:

- the same operational window is still within its 24-hour bound;
- the security-relevant context fingerprint is unchanged;
- the singleton execution-worker invariant still holds; and
- the next required freshness/security probe returns a semantically valid positive result.

This is not interpreted as the worker deciding that a security failure has repaired itself. PAUSED is reserved for cases where no negative security verdict was observed in the first place.

If the first trustworthy response after a PAUSED period is negative or ambiguous, the state becomes LOCKED and the window is invalidated.

### 14. LOCKED always destroys the current authorization window

A LOCKED transition invalidates the current operational window and any in-flight ARM attempt.

The worker must not automatically recover from LOCKED merely because a later probe appears healthy.

Reactivation after LOCKED requires the full explicit cycle:

1. new SUPERADMIN ARM request targeted to the current `worker_id` + `boot_id`;
2. atomic `REQUESTED -> CONSUMED` claim;
3. singleton verification;
4. new signed readiness run;
5. new readiness PASS and attestation;
6. final arm-attempt validity fence;
7. new operational window.

A configuration fix, provider recovery, replica reduction or positive later probe cannot by itself reopen the previous window.

### 15. Conditions that stop RISEx execution

The execution worker must stop issuing RISEx provider POSTs immediately when any of the following applies:

- no operational window exists;
- the window reaches its 24-hour maximum;
- a valid targeted DISARM request supersedes the current control generation or is consumed;
- the worker process restarts;
- the security-relevant context identity changes or no longer matches;
- the singleton invariant is violated;
- gate 1 fails;
- gate 2 fails;
- gate 3 for the explicitly selected authorization mode fails, including a continuous-mode permit account/signer mismatch;
- gate 4 fails;
- the immediate pre-POST freshness probe does not produce the required positive result;
- the mandatory post-freshness continuous submission fence in section 10B fails;
- a process-local cancellation token for the in-flight continuous order has been invalidated;
- a security-negative provider/runtime response is observed;
- a security-relevant response is ambiguous or cannot be trusted.

The same invalidation conditions cancel an in-flight `ARMING` attempt before it can open a window.

A pure reachability failure may place an existing window in PAUSED rather than destroying it, subject to the PAUSED rules above. A reachability failure during ARM readiness does not authorize opening a window; the attempt fails closed unless readiness and the finalization fence complete successfully.

Stopping RISEx execution does not require the whole execution-worker process to terminate. Other provider-independent worker responsibilities may continue if their own controls allow it.

### 16. Restart is a hard authorization boundary

The operational window and in-flight arm attempt are process-local and non-persistent.

A process restart or deployment loses:

- the process-local `boot_id`;
- any arm-attempt object;
- any unconsumed readiness attestation held only in process memory;
- the operational window.

The new worker generates a new `boot_id` and starts with RISEx continuous execution unauthorized.

A persisted `REQUESTED` command targeted to the previous `boot_id` cannot authorize the new process and must not be retargeted automatically.

No environment variable, heartbeat telemetry or persisted request state may cause startup to recreate the previous authorization.

### 17. No persistence of PASS or operational authorization

Neither readiness PASS nor the operational window may be persisted as authorization state in:

- Redis;
- a database;
- a file intended for replay;
- a queue message;
- another shared cache or durable store.

The following may be persisted because they are not authorization inputs:

- one-shot operator ARM/DISARM request, its monotonic control generation and its consumed acknowledgement;
- audit records;
- heartbeat telemetry describing the worker's self-reported process-local state, including the existence of an in-flight arm attempt.

Persisted telemetry must never be accepted by the worker as proof that its own arm attempt or window exists.

## Observability and read-only status

### Heartbeat telemetry published by the worker

While running, each `execution-worker` must publish into its own `WorkerHeartbeat`:

- `worker_id` in the existing primary identity field;
- `seen_at`;
- current job information as already supported;
- `meta.boot_id`;
- `meta.risex.reported_state` = `DISABLED | ARMING | ENABLED | PAUSED | LOCKED`;
- `meta.risex.arm_request_id` while an ARM attempt is in flight;
- `meta.risex.arm_control_generation` while an ARM attempt is in flight;
- `meta.risex.arm_started_at` while an ARM attempt is in flight;
- `meta.risex.opened_at` when a window exists;
- `meta.risex.expires_at` when a window exists;
- `meta.risex.last_transition_at`;
- `meta.risex.last_transition_reason`;
- optionally the last consumed control `request_id` and `control_generation` for correlation, but never a readiness attestation or reusable authorization token.

`ARMING` must be published before the network-bound readiness operation begins and cleared/replaced after the attempt succeeds, fails, is canceled, or is invalidated.

This heartbeat is a **read-only mirror of process-local state**. The worker writes it for observability and must never read it back to reconstruct, extend or authorize an arm attempt or window.

### Read-only administrative endpoint

The implementation must expose:

`GET /admin/risex-execution-status`

The endpoint is read-only and must not arm, disarm, refresh, resume or otherwise mutate the arm attempt or window.

For the singleton case it must return at least:

- `worker_id`;
- `boot_id` as reported telemetry;
- `heartbeat_seen_at`;
- `heartbeat_age_seconds`;
- `reported_state`;
- in-flight `arm_request_id`, `arm_control_generation` and `arm_started_at` when reported state is `ARMING`;
- `opened_at`;
- `expires_at`;
- `remaining_seconds` when determinable;
- `observability`;
- `authorization_status`;
- current control-request metadata sufficient to distinguish `REQUESTED` from `CONSUMED`, including `request_id`, `control_generation` and action, without exposing sensitive readiness material.

For a fresh heartbeat with `reported_state == ARMING`, `authorization_status` must be `PENDING`, never `AUTHORIZED`.

### Mandatory stale-heartbeat rule

A heartbeat is **STALE** when:

`now - WorkerHeartbeat.seen_at > 180 seconds`

This threshold is intentionally much longer than the normal idle heartbeat path. A healthy idle consume loop normally updates after each approximately two-second empty Redis stream read, while the independent maintenance loop updates after its work and normally sleeps 60 seconds between cycles. A heartbeat older than 180 seconds therefore indicates that **both** ordinary observation paths have failed to update PostgreSQL; operationally, the worker is treated as stopped or database-isolated. The threshold does not imply a fixed 60-second heartbeat scheduler and does not measure RISEx authorization freshness.

When the latest relevant heartbeat is stale, the status endpoint must never present a bare `OPEN`, `ENABLED`, `ARMING` or equivalent current-authorization/current-progress claim as trustworthy current state.

It must report all three concepts separately, for example:

```text
reported_state: ENABLED
observability: STALE
authorization_status: UNKNOWN
```

or, for an arm attempt last observed in flight:

```text
reported_state: ARMING
observability: STALE
authorization_status: UNKNOWN
```

`reported_state` means only **the last state the worker reported before observability was lost**.

`authorization_status: UNKNOWN` is mandatory for a stale or missing heartbeat, regardless of whether the persisted telemetry says `ARMING`, `ENABLED`, `PAUSED`, `LOCKED` or `DISABLED`.

`remaining_seconds` must not be represented as trustworthy current authorization duration when observability is stale; the endpoint may return it as null or clearly label a purely historical/computed value, but it must not imply that the window is still active.

For a fresh heartbeat, the endpoint may report `authorization_status: AUTHORIZED` only when all locally observable prerequisites for that status are satisfied, including:

- exactly one live execution-worker heartbeat;
- fresh heartbeat age `<= 180 seconds`;
- `reported_state == ENABLED`;
- `expires_at` is in the future;
- no locally known singleton or control-plane blocker.

This API status is observational only. It does not replace the worker's own process-local window check, the arm finalization fence, gate 1, gate 2, gate 4, the post-freshness submission fence or the mandatory pre-POST freshness probe.

If multiple live replicas exist, the endpoint must make the topology conflict explicit and must not collapse the response into one apparently authorized worker. `authorization_status` must be non-authorized/fail-closed for RISEx.

## State model

The continuous execution authorization state is interpreted as follows:

| State | Meaning | RISEx POST allowed? | Exit / recovery |
| --- | --- | --- | --- |
| `DISABLED` | No valid process-local operational window or active arm attempt exists | No | New targeted ARM request -> atomic consume -> `ARMING` |
| `ARMING` | One consumed ARM is undergoing readiness/finalization; no window exists yet | No | PASS + finalization fence -> `ENABLED`; DISARM/failure/invalidation -> `DISABLED` or `LOCKED` |
| `ENABLED` | Valid window, matching context, singleton invariant holds; per-operation controls may be evaluated | Only after the explicitly selected gate-3 mode, per-order permit binding, unchanged gates 1/2/4, positive freshness and the post-freshness submission fence pass | Continues until expiry, DISARM, restart, invalidation, PAUSED or LOCKED |
| `PAUSED` | Same window exists, but no trustworthy security verdict is obtainable because of pure reachability/availability failure | No | Automatic resume only after a trustworthy positive probe while the same window remains valid and singleton still holds |
| `LOCKED` | Negative, ambiguous, untrusted security evidence or singleton violation invalidated the window/attempt | No | New targeted ARM request + atomic consume + new readiness PASS + finalization fence + new window |

Window expiry, explicit DISARM and process restart return the process to `DISABLED`; a security-negative, ambiguous or multi-replica condition produces `LOCKED`. A DISARM during `ARMING` cancels the attempt and prevents its eventual readiness result from opening a window.

Heartbeat telemetry is not an authorization source. A stale heartbeat changes **observability** to `STALE` and the API's `authorization_status` to `UNKNOWN`; it does not reconstruct or mutate process-local state.

## Relationship to ADR-0002

ADR-0004 does **not** change ADR-0002's session-key authorization criterion, bounded-capital risk acceptance, environment isolation requirements, `fund_movement_path_absent` premise or invalidation conditions.

ADR-0004 clarifies the runtime meaning of a readiness PASS derived from ADR-0002:

- the PASS/attestation is short-lived bootstrap evidence;
- it is not a self-renewing lease for a continuous worker;
- after one-shot consumption into an operational window, continuity is governed by the window's explicit operator intent, process/context binding, singleton requirement, hard 24-hour ceiling, the explicit continuous gate-3 mode and its per-order permit identity binding, plus unchanged gates 1, 2, 4, the mandatory pre-POST freshness probe and the mandatory post-freshness submission fence;
- a readiness PASS cannot override a later operator DISARM or security invalidation that occurred during the ARM attempt.

To the extent that any downstream implementation or documentation interprets the 300-second readiness TTL as the direct lifetime of continuous RISEx worker authorization, that operational interpretation is superseded by ADR-0004.

No other ADR-0002 decision is superseded.

## Relationship to ADR-0003

ADR-0004 does **not** change ADR-0003's conditional applicability of `fund_movement_rejected` and `withdrawal_rejected`, the requirement not to fabricate behavioral evidence, or the fail-closed use of `fund_movement_path_absent`.

ADR-0004 clarifies only the continuous runtime lifecycle around readiness evidence that incorporates the ADR-0003-compatible gate semantics.

To the extent that any downstream implementation or documentation assumes an ADR-0003-compliant readiness PASS must be silently regenerated every 300 seconds solely to keep a continuously running worker authorized, that operational interpretation is superseded by ADR-0004.

No negative-probe decision in ADR-0003 is superseded.

## Mandatory review and earlier invalidation

This ADR must be reviewed no later than **2026-12-13**.

The review date is synchronized with ADR-0002 and ADR-0003 so the RISEx authorization model, negative-probe applicability and continuous-execution lifecycle are reviewed as one security boundary rather than on independent stale schedules.

Review must be brought forward immediately if any of the following occurs before that date:

- the execution-worker deployment topology changes materially;
- `execution-worker` is intentionally scaled beyond one replica;
- the arming mechanism changes from the explicit one-shot `risex_execution_control` request model;
- `control_generation`, request-consumption atomicity or arm-finalization advisory-lock semantics change;
- any proposal introduces persistence or replay of readiness PASS or Operational Execution Window;
- the worker heartbeat cadence or 180-second stale threshold changes materially;
- the readiness attestation TTL or issuance semantics change;
- the gate-3 authorization-mode selection, mutual-exclusion rule or per-order account/signer binding changes materially;
- the post-freshness submission-fence, local serialization or in-flight cancellation semantics change materially;
- the pre-POST freshness probe is moved, weakened or removed;
- gate 1, gate 2 or gate 4 changes materially;
- the RISEx signer/account/network/deployment identity model changes;
- ADR-0002 or ADR-0003 is invalidated or materially revised.

## Consequences

### Positive

- The 300-second readiness TTL keeps a precise purpose instead of becoming decorative.
- The worker cannot silently renew its own authorization.
- An operator can execute ARM/DISARM through the existing application control plane without requiring Railway container shell/exec access.
- The durable control record is only a one-shot request; PASS and window remain process-local and non-persistent.
- Atomic request consumption prevents two processes from claiming the same explicit authorization act.
- Monotonic control generations plus the advisory-lock finalization fence prevent a readiness PASS from opening a window after a later DISARM or invalidation.
- `ARMING` makes the in-flight authorization attempt explicitly observable without making it authorized.
- `boot_id` makes restart distinguishable from the targeted operator action and makes old pending requests non-replayable by a new process incarnation.
- The singleton runtime invariant prevents intermittent per-replica RISEx authorization behavior.
- Restart and security-relevant context changes are hard authorization boundaries.
- Human intent is re-established at least once every 24 hours without assuming an eight-hour staffed shift.
- Gate 3 is explicit and non-fallback: the short-lived path remains attestation-bound, while the continuous path is window-bound and preserves per-order account/signer identity binding.
- The post-freshness submission fence prevents a positive provider probe from being reused after the local continuous authorization changed while the probe was in flight.
- Per-operation freshness, gates 1, 2 and 4, and the explicitly selected gate-3 mode remain authoritative at the point of use.
- PAUSED and LOCKED have explicit fail-closed semantics.
- Stale observability cannot be displayed as current authorization.

### Negative / residual risks

- A live singleton worker may remain authorized for up to 24 hours after a valid explicit arm, subject to all unchanged per-operation gates and freshness checks.
- The current design deliberately does not support RISEx continuous signed execution across multiple execution-worker replicas.
- PostgreSQL availability is required to deliver and atomically consume ARM/DISARM requests, serialize ARM finalization against later control commands and verify the heartbeat-based singleton invariant.
- A 24-hour bound does not guarantee that a window never crosses a night; it guarantees only a maximum one-day authorization horizon.
- Process-local authorization means every deployment or restart requires a new explicit ARM/readiness cycle.
- An ARM request consumed immediately before a process crash is lost by design and must be reissued explicitly.
- A database/control-plane failure during the final arm fence cancels the arm attempt even after readiness PASS; this is intentional fail-closed behavior.
- Incorrect classification of an error as reachability-only could delay LOCKED; the conservative rule therefore classifies malformed, ambiguous and authenticity-related failures as LOCKED.
- Heartbeat telemetry is eventually observed state; after 180 seconds without a fresh heartbeat the administrative view necessarily becomes `UNKNOWN`.
- During a long-running job the consume-loop fast control poll is unavailable; the maintenance task remains an independent periodic observer, and Step 4B must perform both the initial point-of-use window check and the post-freshness submission fence before any RISEx POST.
- An invalidation arriving after the operation has crossed the serialized provider-submission linearization point cannot retroactively unsend a request already being submitted; the serialization rule exists to make that ordering explicit and reviewable.

## Alternatives considered

### POSIX `SIGUSR1` / `SIGUSR2`

Rejected for this deployment.

The current Railway operating path does not expose a usable shell/exec route to the live execution-worker process, and `railway run` executes locally rather than in the deployed container. A signal-based design would therefore specify an operator action that cannot be reliably executed here.

### Automatic periodic rearming

Rejected.

Even with a fixed maximum number of renewals, the worker would still be renewing its own authority. A limit such as twelve five-minute renewals merely moves the problem to sixty minutes and preserves the same conceptual flaw.

### Long or infinite readiness TTL

Rejected.

It collapses readiness evidence and continuous authorization into one durable fact and weakens the protection provided by a bounded readiness observation.

### Implicit gate-3 fallback between attestation and operational window

Rejected.

The shared adapter class may support both authorization lifecycles, but an instance must be constructed in one explicit, immutable gate-3 mode. Inferring the mode from the presence or absence of an attestation/window would allow a misconfigured adapter to silently change authorization semantics. Missing, ambiguous or conflicting gate-3 mode/material must fail closed; there is no fallback from one mode to the other.

### Single pre-gate-4 window check with no post-freshness fence

Rejected.

The mandatory freshness probe is an awaited network operation. A window can expire or be invalidated while that await is in flight, so a check performed only before the probe is stale by the time the provider mutation is attempted. Continuous mode therefore requires the section 10B post-freshness fence plus a serialization boundary with local invalidation. The system must never implement `window check -> await freshness -> POST` as sufficient authorization.

### Level-triggered database flag such as `risex_armed=true` or `system_flags`

Rejected.

A durable boolean would be reread after restart and could automatically re-arm a new process. That merely moves the forbidden persistent authorization pattern from an environment variable into PostgreSQL. `system_flags` also models level-triggered state rather than the one-shot request/claim/generation lifecycle required here.

The accepted database use is a dedicated edge-triggered, request-id-bearing, target-bound one-shot row in `risex_execution_control` that becomes permanently `CONSUMED` before readiness runs.

### Unlocked final readiness check before window creation

Rejected.

Reading the control rows and later opening the process-local window leaves a check-then-use interval in which a DISARM can be accepted and then ignored by the in-flight ARM result. Locking only the consumed ARM row also fails because a DISARM is a new row insert. The accepted design serializes operator command creation and ARM finalization with the same transaction-scoped advisory lock keyed by `worker_id` + `boot_id`, and treats any mismatch or failure as cancellation.

### Multi-replica process-local windows

Rejected for ADR-0004.

Independent per-replica windows would create intermittent behavior depending on which replica claims a job. A distributed authorization design would require a different security model and a new ADR.

### Eight-hour operational window

Rejected as the default.

It assumes a staffed-shift model that the 24/7 worker does not have and would create repeated daily manual rearming pressure likely to encourage automation of the control.

### Seventy-two-hour operational window

Rejected.

It permits one explicit act to authorize multiple daily operating cycles and unattended periods, which is too weak a bound for the intended human-intent control.

### Persisted readiness PASS or Operational Execution Window in Redis or the database

Rejected.

Persistence would survive process restart and would convert a process-bound authorization into replayable shared state, contrary to the fail-closed restart boundary.

This rejection does not apply to the one-shot operator request, monotonic control generation, consumed acknowledgement, audit log or heartbeat telemetry because none of those may be used as readiness evidence or a persisted execution window.

## Implementation boundary

ADR-0004 is accepted together with the Step 4A TDD implementation of the Operational Execution Window.

Step 4A implements:

- additive migration `0013_risex_execution_control` containing only the dedicated control table and indexes on that new table;
- one-shot ARM/DISARM command creation and atomic consumption;
- process-local `boot_id`, arm attempt and Operational Execution Window state;
- the shared PostgreSQL advisory-lock finalization fence;
- runtime singleton verification from live worker heartbeats;
- heartbeat telemetry and stale-observability rules;
- SUPERADMIN ARM/DISARM and read-only admin status endpoints.

Step 4A does **not** route RISEx jobs through `_process_job_locked()` or otherwise make the new window an execution path. Hyperliquid execution behavior is unchanged. That point-of-use integration is Step 4B and must verify the process-local window before entering the existing RISEx per-operation authorization path, must perform the mandatory section 10B post-freshness submission fence immediately before provider POST, and must not infer authorization from a previous control-channel poll or from heartbeat telemetry.

The readiness PASS and Operational Execution Window remain non-persistent. PostgreSQL stores only operator requests/consumption acknowledgements and observational telemetry.

## Related evidence and decisions

- `docs/adr/ADR-0002-risex-session-key-authorization-model.md`
- `docs/adr/ADR-0003-risex-fund-movement-negative-probe-applicability.md`
- `docs/superpowers/specs/2026-09-13-risex-signed-execution-orchestration-design.md`
- `backend/app/models/entities.py` (`RISExExecutionControl`, `WorkerHeartbeat`)
- `backend/alembic/versions/0013_risex_execution_control.py`
- `backend/app/services/risex_execution_control.py`
- `backend/app/services/risex_execution_window.py`
- `backend/app/api/admin.py`
- `backend/app/workers/execution_worker.py`
- `backend/app/security/risex_signed_testnet_runner.py`
- `backend/app/services/risex_signed_execution.py`
- `backend/app/adapters/risex.py`

---

## Amendment: Accepted — multi-user runtime window and per-user credentials

**Status**: Accepted — Implemented in PR #217
**Decision date**: 2026-09-23
**Transitional rule**: PR #217 implements this amendment for continuous multi-user execution. The pre-existing single-account semantics remain authoritative only for the unchanged manual/test path described in section 2A, mode 1.
**Acceptance rule**: This amendment becomes Accepted/operative only in the implementation PR that implements it; if implementation materially differs from this proposal, it remains Proposed and requires further amendment.

This amendment does not alter the top-level Status of ADR-0004, which remains **Accepted**. PR #217 implements the continuous multi-user authorization model described below, supporting multiple end users with per-user RISEx credentials while preserving the fail-closed controls defined by this ADR. The unchanged manual/test path remains governed by section 2A, mode 1.

### A. Window/Fingerprint scope

Under this amendment, the `RISEx Operational Execution Window` and its security-relevant context fingerprint (section 10) continue to authorize **worker-global runtime only**. The window is not, and does not become, a per-user construct.

The fingerprint itself includes:

- `worker_id`;
- process-local `boot_id`;
- application build/deployment identity;
- provider/network identity;
- API/RPC/transport configuration;
- pinned deployment identity;
- global signed-write and security configuration;
- the worker-global ARM assertion `operatorhub_bypass_disabled`.

The following state is bound to the operational window and is verified at every relevant point of use, but is deliberately **not** part of the fingerprint hash:

- the execution-worker singleton invariant: `_singleton_matches_worker()` is checked during ARM finalization in `risex_execution_worker_extension.py`, again for each RISEx CopyJob, and again by the final authorizer in `execution_worker.py`;
- control generation: `_poll_risex_control_once()` computes the fingerprint before `begin_arm()`, while `begin_arm()` advances `last_control_generation`; finalization is instead protected by `arm_finalization_fence()` and `RISExOperationalWindowController.can_finalize_arm()`;
- `authorization_invalidation_epoch`: `can_finalize_arm()` compares the ARM attempt snapshot with the current invalidation epoch, and the continuous writer snapshots and re-checks the same epoch in `RISExAdapter._continuous_final_fence()` immediately before submission.

Hashing this state would not add protection beyond those explicit live fences and would make legitimate authorization transitions invalidate the fingerprint mechanically. In particular, hashing control generation would make the ARM that created the fingerprint disagree with itself after `begin_arm()` advances the generation.

Runtime kill-switch values such as `global_pause` and `emergency_stop` are also **not** fingerprint material. They are read and enforced per order by the RISEx worker risk path. A pause therefore blocks or constrains order execution according to the risk rules without destroying the otherwise valid operational window.

Once this amendment is implemented, the fingerprint **excludes**:

- per-user account identity;
- per-user signer/session-key identity;
- per-user private key material;
- per-user credential id or credential version;
- per-user credential expiry;
- per-user permission scope.

Account, signer, credential and permission identity move out of the worker-global window and into per-order, per-user authorization as described in section B below.

### B. Per-user authorization

Under this amendment, every RISEx order resolves its credential independently, at order time, from:

- `CopyJob.user_id`; and
- the exact active `ExecutionEpoch` for that job.

Resolution requires an exact match on `provider = risex` together with the matching `account_address` and `credential_version` recorded for that user/epoch. The credential is decrypted only at the point of use; the signer is derived from the decrypted key immediately before use and is never cached in a persistent, reusable form.

Before any provider POST, the order path must:

1. validate that the resolved account/signer are bound to the exact `CopyJob.user_id` and active `ExecutionEpoch`;
2. check a fresh, authoritative on-chain query for active/unexpired/required permissions, fail-closed on any negative, ambiguous or unreachable-with-doubt result;
3. guarantee that a credential superseded by credential rotation or by a destination change is never used for a provider POST, even if its on-chain session remains active. The worker's final verification that the job's exact `ExecutionEpoch` is still the user's active epoch and that its `credential_version` still matches the generation of the currently stored RISEx signing credential, and the transactions that rotate that credential or change the execution destination, must be serialized by a database-level mechanism shared by the API and worker processes. A process-local lock, including the section 10B submission boundary, is insufficient. Only two outcomes are valid: (a) the credential rotation or destination change completes first, so the final worker verification observes the superseding state and no POST is issued; or (b) the final worker verification completes first, so the credential rotation or destination change is rejected until execution for that epoch is resolved. No database lock used for this serialization may remain held across the RISEx network call. Rejecting credential rotation does not remove any emergency control: strategy pause and kill switches remain available, and credential rotation is not an emergency control. Concurrency tests must use separate database sessions rather than a shared in-memory lock, exercise both orderings (a) and (b), and cover both credential rotation and destination change;
4. re-check the worker-global window and the final local fences defined in sections 8A/10B before the POST is issued.

**Key principle**: the worker-global window gate and the per-user credential gate are independent controls. Both must pass. Neither may substitute for the other, and neither may be inferred from the other's success.

### C. Security rationale

Per-order, point-of-use account/signer verification, performed against the exact credential resolved for that `user_id` + `ExecutionEpoch`, is at least as strong a security property as the current ARM-time identity binding described in section 10A, because it:

- verifies the exact credential actually used for that specific order, rather than a credential bound at window-open time; and
- is temporally closer to the provider POST than any check performed at ARM/finalization time, which narrows the window in which a change to permissions or credential state could go undetected.

The worker-global ARM/window mechanism continues to protect the properties that are inherently global and process-scoped, and this amendment does not weaken any of them:

- explicit operator intent (ARM/DISARM);
- process/boot identity (`boot_id`);
- restart as a hard authorization boundary;
- the execution-worker singleton invariant;
- ARM/DISARM ordering and the finalization fence (section 8A);
- deployment/runtime integrity (build, provider, network, transport configuration);
- the bounded 24-hour window.

### D. Realign ADR wording

Implementing this amendment realigns the wording of ADR-0004 as follows:

- the runtime epoch (the `RISEx Operational Execution Window` and its fingerprint) becomes explicitly **worker-global**, not account-scoped;
- account identity, signer identity and permission verification become an explicit **per-user, per-order authorization** step, resolved from `CopyJob.user_id` and the active `ExecutionEpoch`, independent of the worker-global window;
- the existing short-lived manual/test attestation mode (section 2A, mode 1) is preserved **unchanged**: it remains account/signer-bound and its 300-second TTL remains fully enforced. This amendment does not modify the manual/test attestation path.

### E. Current ARM assertions (exact implementation)

The current, exactly-implemented set of hard-required assertions for single-account continuous ARM readiness is:

- `disposable_account_asserted`;
- `dedicated_signer_asserted`;
- `operatorhub_bypass_disabled`;
- `fund_movement_path_absent`.

For the unchanged manual/test path in section 2A, mode 1, these four single-account assertions remain authoritative. For continuous multi-user execution, PR #217 implements the worker-global assertion set described below.

**Current continuous multi-user behavior (implemented in PR #217)**:

- **`disposable_account_asserted`** — DROPS from worker-global readiness. This assertion is specific to the single test/faucet-funded account model and has no equivalent meaning once accounts are per-user.
- **`dedicated_signer_asserted`** — DROPS as a worker-global operator assertion. A dedicated signer per user remains **mandatory**, governed by ADR-0006 §0, and is verified per order from the stored per-user credential plus the authoritative account/signer binding described in section B, rather than asserted once at worker-global ARM time.
- **`fund_movement_path_absent`** — DROPS as a worker-global assertion. Real-capital `MoveFund` risk for multi-user continuous execution is governed by ADR-0006 §1, together with the current per-user, per-order permission evidence obtained at point of use (section B, step 2).
- **`operatorhub_bypass_disabled`** — REMAINS a worker-global assertion, unchanged.
- The existing forbidden main-wallet-key protections remain worker-global and fail-closed, unchanged by this amendment.

### F. Global continuous readiness

The worker-global continuous readiness implemented in PR #217 has no worker-global signer and must, at minimum:

- establish an explicit ARM for the exact `worker_id` + `boot_id` incarnation, as in section 7;
- include a fresh heartbeat/identity check, the singleton invariant (section 9), deployment/build identity and provider/network identity;
- include the pinned deployment preflight and API/RPC/transport configuration checks;
- include the signed-write/kill-switch gates, `operatorhub_bypass_disabled`, and the global finalization fences described in sections 8A and 10B.

Such a readiness check **MUST NOT** load or require a worker-global user RISEx account or signer. Per-user account/signer/permission verification is performed exclusively at the per-order authorization step described in section B.

### G. Existing single-account assertion path

The existing `run_signed_testnet_readiness()` single-account assertion path and its four single-account assertions remain authoritative and unchanged for the manual/test path in section 2A, mode 1.

PR #217 provides the signerless worker-global continuous readiness path described in section F while preserving the short-lived manual/test attestation semantics of section 2A, mode 1, unchanged.

### H. MoveFund (ADR-0002 / ADR-0003)

- The ADR-0002 and ADR-0003 testnet assertions, including `fund_movement_path_absent` and the negative-probe applicability rules, remain relevant and unchanged for the isolated manual/test readiness model.
- For real-user, multi-user continuous execution, ADR-0006 §1 governs `MoveFund`/real-capital risk, as described in section E above.
- Worker-global operator assertions never substitute for the current, fresh, on-chain per-user permission evidence required at point of use (section B).

### I. Implementation boundary

This amendment is implemented by PR #217 through runtime code and tests without migrations, workflow changes, environment changes or deployment actions. The main body of ADR-0004 remains authoritative except where sections 10 and 10A explicitly defer to this Accepted multi-user amendment for continuous execution.

PR #217 is the dedicated TDD implementation PR that makes this amendment Accepted and operative for continuous multi-user execution.
