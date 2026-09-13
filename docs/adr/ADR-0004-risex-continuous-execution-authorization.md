# ADR-0004 — RISEx continuous execution authorization

- **Status:** Proposed
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

The repository already has PostgreSQL `system_flags` with operator/audit metadata. ADR-0004 uses that existing database-backed control channel for **requests only**. Readiness PASS and the Operational Execution Window remain non-persistent.

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
- multiple worker replicas having inconsistent process-local authorization state and therefore intermittently accepting or rejecting equivalent RISEx work.

ADR-0004 addresses those gaps without replacing the per-operation gates or freshness probe.

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
5. one-shot consumption of that new attestation into the new process-local window.

No automatic or background rearming is permitted.

### 3. Every worker process gets a fresh process-local `boot_id`

At each `execution-worker` process start, the worker must generate a cryptographically random `boot_id` in memory.

The `boot_id` identifies that exact process incarnation and is distinct from the stable-per-replica `worker_id` / `RAILWAY_REPLICA_ID`.

The worker publishes the `boot_id` only as telemetry in its own `WorkerHeartbeat.meta`.

The `boot_id` must never be loaded from:

- an environment variable;
- `system_flags`;
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

### 4. Concrete arming/disarming mechanism: one-shot `system_flags` request

The selected control channel is the existing PostgreSQL `system_flags` table.

The fixed control slug is:

`risex_execution_control`

The row is a **command mailbox**, not an authorization flag. Its JSON `value` must contain at least:

```text
request_id: UUID
action: ARM | DISARM
target_worker_id: string
target_boot_id: UUID
state: REQUESTED | CONSUMED
requested_at: timestamp
consumed_at: timestamp | null
consumed_by_worker_id: string | null
consumed_by_boot_id: UUID | null
```

Existing `SystemFlag.updated_by`, `reason`, `created_at` and `updated_at` provide operator identity, reason and audit timestamps.

`enabled` may be used as an implementation convenience to mirror whether a command is pending, but **authorization must never be inferred from `enabled`**. The authoritative request lifecycle is `value.state` plus request/target identity.

A new operator command must use a new random `request_id`.

The control path must reject overwriting an unconsumed `REQUESTED` command. A pending command must first be consumed or explicitly superseded through a separately audited operation defined by the implementation; silent replacement is forbidden.

### 5. Operator API for ARM and DISARM

The implementation must expose a CSRF-protected SUPERADMIN write endpoint dedicated to this control action:

`POST /admin/risex-execution-control`

The request must explicitly contain:

- `action` = `ARM` or `DISARM`;
- `target_worker_id`;
- `target_boot_id`;
- operator confirmation text appropriate to the action;
- `reason`.

The endpoint must validate before creating the request that:

- the target heartbeat is currently fresh;
- the supplied `target_worker_id` and `target_boot_id` match the currently reported live process incarnation;
- exactly one live `execution-worker` replica exists under the singleton rule below;
- there is no existing unconsumed `REQUESTED` control command.

The endpoint creates a new `request_id`, writes the one-shot `REQUESTED` command and records the operator audit event.

The endpoint does **not** run readiness, open a window or claim success merely because the request was persisted.

Its successful response means only:

> the explicit operator request was accepted into the control channel for the targeted live process incarnation.

### 6. Request consumption must be atomic and one-shot

A worker must never implement command consumption as an unlocked `SELECT` followed later by an independent `UPDATE`.

Consumption must be one indivisible database state transition so that two processes cannot consume the same `request_id`.

The implementation must use one of these equivalent atomic patterns:

1. `SELECT ... FOR UPDATE` on `risex_execution_control` inside one database transaction, verify all claim predicates while holding the row lock, mutate `REQUESTED -> CONSUMED`, then commit; or
2. a single conditional `UPDATE ... WHERE state = 'REQUESTED' AND request_id = ... AND target_worker_id = ... AND target_boot_id = ...` and require exactly one affected row.

The claim predicates must include at least:

- `state == REQUESTED`;
- expected `request_id`;
- `target_worker_id == self.worker_id`;
- `target_boot_id == self.boot_id`.

The worker must persist `CONSUMED`, `consumed_at`, `consumed_by_worker_id` and `consumed_by_boot_id` **before** executing readiness or changing the process-local window.

The transaction must commit before any potentially slow provider/readiness network operation begins.

Therefore a crash after command consumption but before readiness completion cannot cause the same request to be retried automatically. Another attempt requires a new explicit operator request.

This is intentional fail-closed behavior.

### 7. ARM request semantics

After atomically consuming an ARM request, the targeted worker performs exactly one authorization attempt.

Before readiness can open a window, the worker must verify the singleton precondition and all required readiness prerequisites.

If readiness:

- passes, one new readiness attestation may be consumed into one new process-local operational window;
- fails;
- times out;
- raises;
- cannot establish the singleton precondition; or
- cannot produce a valid attestation,

then the already-consumed ARM request remains consumed and **no window opens**.

A new attempt always requires a new operator ARM request with a new `request_id`.

The worker must not poll a consumed ARM request and must not regenerate readiness from it.

### 8. DISARM request semantics

A DISARM request is targeted and consumed through the same atomic mechanism.

After atomic consumption, if the request targets the worker's current `worker_id` + `boot_id`, that worker immediately closes any current operational window and transitions to `DISABLED`.

DISARM does not require readiness.

If the target process has already restarted, its previous window has already been destroyed by the restart boundary. The new process must not consume the old DISARM request because its `boot_id` differs.

### 9. RISEx requires an execution-worker singleton

RISEx continuous signed execution has an explicit operational precondition:

> exactly one live `execution-worker` replica may exist.

This is a real limitation of the current design because the Operational Execution Window is intentionally process-local. Supporting multiple replicas would require a separate distributed authorization design and is outside ADR-0004.

Railway configuration currently declares one replica, but configuration alone is insufficient. The worker must verify the singleton invariant at runtime from `WorkerHeartbeat` records.

For ADR-0004, an `execution-worker` heartbeat is **live** only when:

- `service == 'execution-worker'`; and
- `now - seen_at <= 180 seconds`.

The worker must publish a heartbeat at least once per normal 60-second operational/maintenance cadence, including promptly after process start. The 180-second live/stale threshold represents three missed 60-second heartbeat opportunities. It is an operational liveness threshold, not freshness of RISEx authorization.

The singleton invariant must be checked:

- before accepting an ARM request at the SUPERADMIN API;
- by the worker after atomically consuming ARM and before readiness opens a window;
- continuously while a window exists, no less often than the normal heartbeat/maintenance cycle; and
- at the final local authorization check before a RISEx execution attempt may proceed to the existing per-operation gates.

If more than one live `execution-worker` heartbeat is observed, RISEx execution is fail-closed and any existing window must transition to `LOCKED` and be invalidated.

If zero live worker heartbeats can be established by the administrative control/read model, the system must not claim an executable authorization state.

Scaling `execution-worker` above one replica therefore requires a new ADR or an explicit revision of ADR-0004 before RISEx signed continuous execution can remain enabled.

### 10. Bind the window to the security-relevant runtime context

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

Any change or mismatch in a security-relevant context component invalidates the current window.

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

A LOCKED transition invalidates the current operational window.

The worker must not automatically recover from LOCKED merely because a later probe appears healthy.

Reactivation after LOCKED requires the full explicit cycle:

1. new SUPERADMIN ARM request targeted to the current `worker_id` + `boot_id`;
2. atomic `REQUESTED -> CONSUMED` claim;
3. singleton verification;
4. new signed readiness run;
5. new readiness PASS and attestation;
6. new operational window.

A configuration fix, provider recovery, replica reduction or positive later probe cannot by itself reopen the previous window.

### 15. Conditions that stop RISEx execution

The execution worker must stop issuing RISEx provider POSTs immediately when any of the following applies:

- no operational window exists;
- the window reaches its 24-hour maximum;
- a valid targeted DISARM request is consumed;
- the worker process restarts;
- the security-relevant context identity changes or no longer matches;
- the singleton invariant is violated;
- gate 1 fails;
- gate 2 fails;
- gate 4 fails;
- the immediate pre-POST freshness probe does not produce the required positive result;
- a security-negative provider/runtime response is observed;
- a security-relevant response is ambiguous or cannot be trusted.

A pure reachability failure may place RISEx execution in PAUSED rather than destroying the window, subject to the PAUSED rules above.

Stopping RISEx execution does not require the whole execution-worker process to terminate. Other provider-independent worker responsibilities may continue if their own controls allow it.

### 16. Restart is a hard authorization boundary

The operational window is process-local and non-persistent.

A process restart or deployment loses:

- the process-local `boot_id`;
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

- one-shot operator ARM/DISARM request and its consumed acknowledgement;
- audit records;
- heartbeat telemetry describing the worker's self-reported process-local state.

Persisted telemetry must never be accepted by the worker as proof that its own window exists.

## Observability and read-only status

### Heartbeat telemetry published by the worker

While running, each `execution-worker` must publish into its own `WorkerHeartbeat`:

- `worker_id` in the existing primary identity field;
- `seen_at`;
- current job information as already supported;
- `meta.boot_id`;
- `meta.risex.reported_state` = `DISABLED | ENABLED | PAUSED | LOCKED`;
- `meta.risex.opened_at` when a window exists;
- `meta.risex.expires_at` when a window exists;
- `meta.risex.last_transition_at`;
- `meta.risex.last_transition_reason`;
- optionally the last consumed control `request_id` for correlation, but never a readiness attestation or reusable authorization token.

This is a **read-only mirror of process-local state**. The worker writes it for observability and must never read it back to reconstruct, extend or authorize a window.

### Read-only administrative endpoint

The implementation must expose:

`GET /admin/risex-execution-status`

The endpoint is read-only and must not arm, disarm, refresh, resume or otherwise mutate the window.

For the singleton case it must return at least:

- `worker_id`;
- `boot_id` as reported telemetry;
- `heartbeat_seen_at`;
- `heartbeat_age_seconds`;
- `reported_state`;
- `opened_at`;
- `expires_at`;
- `remaining_seconds` when determinable;
- `observability`;
- `authorization_status`;
- current control-request metadata sufficient to distinguish `REQUESTED` from `CONSUMED`, including `request_id` and action, without exposing sensitive readiness material.

### Mandatory stale-heartbeat rule

A heartbeat is **STALE** when:

`now - WorkerHeartbeat.seen_at > 180 seconds`

The 180-second threshold is three missed 60-second heartbeat opportunities under the required normal worker heartbeat cadence.

When the latest relevant heartbeat is stale, the status endpoint must never present a bare `OPEN`, `ENABLED` or equivalent current-authorization claim.

It must report all three concepts separately, for example:

```text
reported_state: ENABLED
observability: STALE
authorization_status: UNKNOWN
```

`reported_state` means only **the last state the worker reported before observability was lost**.

`authorization_status: UNKNOWN` is mandatory for a stale or missing heartbeat, regardless of whether the persisted telemetry says `ENABLED`, `PAUSED`, `LOCKED` or `DISABLED`.

`remaining_seconds` must not be represented as trustworthy current authorization duration when observability is stale; the endpoint may return it as null or clearly label a purely historical/computed value, but it must not imply that the window is still active.

For a fresh heartbeat, the endpoint may report `authorization_status: AUTHORIZED` only when all locally observable prerequisites for that status are satisfied, including:

- exactly one live execution-worker heartbeat;
- fresh heartbeat age `<= 180 seconds`;
- `reported_state == ENABLED`;
- `expires_at` is in the future;
- no locally known singleton or control-plane blocker.

This API status is observational only. It does not replace the worker's own process-local window check, gate 1, gate 2, gate 4 or the mandatory pre-POST freshness probe.

If multiple live replicas exist, the endpoint must make the topology conflict explicit and must not collapse the response into one apparently authorized worker. `authorization_status` must be non-authorized/fail-closed for RISEx.

## State model

The continuous execution authorization state is interpreted as follows:

| State | Meaning | RISEx POST allowed? | Exit / recovery |
| --- | --- | --- | --- |
| `DISABLED` | No valid process-local operational window exists | No | New targeted ARM request -> atomic consume -> readiness PASS -> new window |
| `ENABLED` | Valid window, matching context, singleton invariant holds; per-operation controls may be evaluated | Only after all existing gates and freshness checks pass | Continues until expiry, DISARM, restart, invalidation, PAUSED or LOCKED |
| `PAUSED` | Same window exists, but no trustworthy security verdict is obtainable because of pure reachability/availability failure | No | Automatic resume only after a trustworthy positive probe while the same window remains valid and singleton still holds |
| `LOCKED` | Negative, ambiguous, untrusted security evidence or singleton violation invalidated the window | No | New targeted ARM request + atomic consume + new readiness PASS + new window |

Window expiry, explicit DISARM and process restart return the process to `DISABLED`; a security-negative, ambiguous or multi-replica condition produces `LOCKED`.

Heartbeat telemetry is not a fifth authorization state. A stale heartbeat changes **observability** to `STALE` and the API's `authorization_status` to `UNKNOWN`; it does not reconstruct or mutate process-local state.

## Relationship to ADR-0002

ADR-0004 does **not** change ADR-0002's session-key authorization criterion, bounded-capital risk acceptance, environment isolation requirements, `fund_movement_path_absent` premise or invalidation conditions.

ADR-0004 clarifies the runtime meaning of a readiness PASS derived from ADR-0002:

- the PASS/attestation is short-lived bootstrap evidence;
- it is not a self-renewing lease for a continuous worker;
- after one-shot consumption into an operational window, continuity is governed by the window's explicit operator intent, process/context binding, singleton requirement, hard 24-hour ceiling and the unchanged per-operation controls.

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
- the arming mechanism changes from the explicit one-shot `system_flags` request model;
- the request-consumption transaction or atomic claim semantics change;
- any proposal introduces persistence or replay of readiness PASS or Operational Execution Window;
- the worker heartbeat cadence or 180-second stale threshold changes materially;
- the readiness attestation TTL or issuance semantics change;
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
- `boot_id` makes restart distinguishable from the targeted operator action and makes old pending requests non-replayable by a new process incarnation.
- The singleton runtime invariant prevents intermittent per-replica RISEx authorization behavior.
- Restart and security-relevant context changes are hard authorization boundaries.
- Human intent is re-established at least once every 24 hours without assuming an eight-hour staffed shift.
- Per-operation freshness and existing gates remain authoritative at the point of use.
- PAUSED and LOCKED have explicit fail-closed semantics.
- Stale observability cannot be displayed as current authorization.

### Negative / residual risks

- A live singleton worker may remain authorized for up to 24 hours after a valid explicit arm, subject to all unchanged per-operation gates and freshness checks.
- The current design deliberately does not support RISEx continuous signed execution across multiple execution-worker replicas.
- PostgreSQL availability is required to deliver and atomically consume ARM/DISARM requests and to verify the heartbeat-based singleton invariant.
- A 24-hour bound does not guarantee that a window never crosses a night; it guarantees only a maximum one-day authorization horizon.
- Process-local authorization means every deployment or restart requires a new explicit ARM/readiness cycle.
- An ARM request consumed immediately before a process crash is lost by design and must be reissued explicitly.
- Incorrect classification of an error as reachability-only could delay LOCKED; the conservative rule therefore classifies malformed, ambiguous and authenticity-related failures as LOCKED.
- Heartbeat telemetry is eventually observed state; after 180 seconds without a fresh heartbeat the administrative view necessarily becomes `UNKNOWN`.

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

### Level-triggered database flag such as `risex_armed=true`

Rejected.

A durable boolean would be reread after restart and could automatically re-arm a new process. That merely moves the forbidden persistent authorization pattern from an environment variable into PostgreSQL.

The accepted database use is instead an edge-triggered, request-id-bearing, target-bound one-shot command that becomes permanently `CONSUMED` before readiness runs.

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

This rejection does not apply to the one-shot operator request, consumed acknowledgement, audit log or heartbeat telemetry because none of those may be used as authorization evidence.

## Implementation boundary

This ADR PR is decision-only.

It must not modify:

- execution-worker runtime code;
- readiness code;
- the adapter or signed transport;
- gate 1, gate 2 or gate 4;
- the pre-POST freshness probe;
- tests or fixtures;
- Railway configuration;
- workflow or ruleset configuration;
- Redis or database models.

A separate TDD implementation PR is required. The implementation may use the existing `system_flags` and `worker_heartbeats` schemas and add the dedicated SUPERADMIN/read-only API behavior required by this ADR; any schema change not necessary to those existing structures requires separate review.

Following the same lifecycle used for ADR-0003, this ADR remains **Proposed** in the documentation-only PR. The implementation PR may change the status to **Accepted** only in the same reviewed GREEN change that implements and verifies the decision. Acceptance must not be performed by this documentation-only PR.

## Related evidence and decisions

- `docs/adr/ADR-0002-risex-session-key-authorization-model.md`
- `docs/adr/ADR-0003-risex-fund-movement-negative-probe-applicability.md`
- `docs/superpowers/specs/2026-09-13-risex-signed-execution-orchestration-design.md`
- `backend/app/models/entities.py` (`SystemFlag`, `WorkerHeartbeat`)
- `backend/app/api/admin.py`
- `backend/app/workers/execution_worker.py`
- `backend/app/security/risex_signed_testnet_runner.py`
- `backend/app/services/risex_signed_execution.py`
- `backend/app/adapters/risex.py`
