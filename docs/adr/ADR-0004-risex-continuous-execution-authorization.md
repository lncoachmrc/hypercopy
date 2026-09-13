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

1. **readiness evidence** — a recent observation that the current RISEx runtime context passed the signed readiness checks; and
2. **continuous execution authorization** — explicit operator intent that a specific live worker process may continue attempting RISEx execution while its security-relevant context remains unchanged and all per-operation controls continue to pass.

ADR-0004 defines that second concept as a process-local **RISEx Operational Execution Window**.

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
- continuous execution persisting indefinitely without periodic human reaffirmation of intent.

ADR-0004 addresses those gaps without replacing the per-operation gates or freshness probe.

## Decision

### 1. Separate readiness evidence from continuous execution authorization

`RISExRuntimeReadinessAttestation` remains a short-lived readiness proof with a 300-second TTL.

A valid readiness attestation may be consumed once to open a process-local `RISEx Operational Execution Window`.

The operational window is a distinct authorization object with different semantics:

- readiness answers **“did this runtime context recently pass the signed readiness checks?”**;
- the operational window answers **“did an operator explicitly authorize this specific live worker process to keep attempting RISEx execution during this bounded operational epoch?”**.

The operational window never substitutes for gate 1, gate 2, gate 4 or the immediate pre-POST freshness probe.

### 2. The readiness attestation is one-shot for window creation

A readiness attestation used to open an operational window is consumed atomically and cannot be reused to open another window.

Closing, expiring or invalidating a window does not make its original readiness attestation reusable, even if the 300-second readiness TTL has not yet elapsed.

Opening a later window requires:

1. a new explicit arming act;
2. a new signed readiness run;
3. a new readiness PASS and attestation;
4. one-shot consumption of that new attestation into the new window.

No automatic or background rearming is permitted.

### 3. Explicit arming mechanism

Explicit arming is an act performed against the **already running worker process**. It must be distinguishable from process startup or restart.

The selected mechanism is a POSIX process signal on the Railway/Linux worker:

- `SIGUSR1` means **request one arm attempt**;
- `SIGUSR2` means **explicitly disarm and close the current operational window immediately**.

An authorized operator sends the signal to the live execution-worker PID from an interactive administrative session on the running service/container.

A restart, deployment, environment-variable load or process boot does **not** emit either signal and therefore cannot arm the worker.

The arming mechanism must not be exposed through:

- an environment variable;
- startup configuration;
- a database row;
- Redis or another cache;
- an ordinary HTTP/API endpoint;
- the normal job queue;
- an automatic timer or background refresh task.

Receiving `SIGUSR1` does not itself authorize any RISEx POST. It creates exactly one in-memory arm attempt tied to the current process boot identity. That attempt triggers one signed readiness evaluation. The arm request is consumed whether the readiness attempt passes or fails.

If readiness fails, times out, raises or cannot produce a valid attestation, no operational window opens. Another attempt requires another explicit `SIGUSR1` from an operator.

This makes every rearm observable, bounded and causally attributable to an operator action rather than to worker lifetime.

The worker must log arm request receipt, arm success/failure, disarm, window open/close, expiry and invalidation events. Those logs are audit evidence only; they must not contain or persist a reusable authorization capability.

Only principals with administrative access to the running worker environment are permitted to send the arming/disarming signals. Application code must not self-signal as part of normal control flow.

### 4. Bind the window to the security-relevant runtime context

An operational window is valid only for the process and context in which it was opened.

Its context identity must include at least:

- process boot identity;
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

### 5. Maximum operational window: 24 hours

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
- readiness refresh;
- a process restart.

There are zero automatic renewals.

When the 24-hour maximum is reached, RISEx execution becomes unauthorized until a new explicit arm/readiness/window cycle completes.

### 6. PAUSED versus LOCKED is determined by evidence quality, not optimism

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
- explicit provider rejection on the security dimension being checked;
- a syntactically received but malformed or semantically incomplete response where the required security state cannot be established.

TLS/authenticity failures are not treated as harmless reachability failures. If the worker cannot establish that it is communicating with the expected authenticated endpoint, the result is LOCKED.

### 7. PAUSED may auto-resume only because no negative security fact was observed

A PAUSED window remains the same operational window and does not create or renew authorization.

The worker may resume RISEx execution automatically from PAUSED only if:

- the same operational window is still within its 24-hour bound;
- the security-relevant context fingerprint is unchanged; and
- the next required freshness/security probe returns a semantically valid positive result.

This is not interpreted as the worker deciding that a security failure has repaired itself. PAUSED is reserved for cases where no negative security verdict was observed in the first place.

If the first trustworthy response after a PAUSED period is negative or ambiguous, the state becomes LOCKED and the window is invalidated.

### 8. LOCKED always destroys the current authorization window

A LOCKED transition invalidates the current operational window.

The worker must not automatically recover from LOCKED merely because a later probe appears healthy.

Reactivation after LOCKED requires the full explicit cycle:

1. operator `SIGUSR1`;
2. new signed readiness run;
3. new readiness PASS and attestation;
4. new operational window.

A configuration fix, provider recovery or positive later probe cannot by itself reopen the previous window.

### 9. Conditions that stop RISEx execution

The execution worker must stop issuing RISEx provider POSTs immediately when any of the following applies:

- no operational window exists;
- the window reaches its 24-hour maximum;
- the operator explicitly disarms via `SIGUSR2`;
- the worker process restarts;
- the security-relevant context identity changes or no longer matches;
- gate 1 fails;
- gate 2 fails;
- gate 4 fails;
- the immediate pre-POST freshness probe does not produce the required positive result;
- a security-negative provider/runtime response is observed;
- a security-relevant response is ambiguous or cannot be trusted.

A pure reachability failure may place RISEx execution in PAUSED rather than destroying the window, subject to the PAUSED rules above.

Stopping RISEx execution does not require the whole execution-worker process to terminate. Other provider-independent worker responsibilities may continue if their own controls allow it.

### 10. Restart is a hard authorization boundary

The operational window is process-local and non-persistent.

A process restart or deployment loses:

- the current arm state;
- any unconsumed readiness attestation held only in process memory;
- the operational window.

The worker must start with RISEx continuous execution unauthorized.

No environment variable or persisted state may cause startup to recreate the previous authorization.

### 11. No persistence of PASS or operational authorization

Neither readiness PASS nor the operational window may be persisted as authorization state in:

- Redis;
- a database;
- a file intended for replay;
- a queue message;
- another shared cache or durable store.

Operational logs may persist facts such as timestamps, reasons and state transitions for auditability, but those records must never be accepted as authorization input.

## State model

The continuous execution authorization state is interpreted as follows:

| State | Meaning | RISEx POST allowed? | Exit / recovery |
| --- | --- | --- | --- |
| `DISABLED` | No valid operational window exists | No | Explicit `SIGUSR1` + new readiness PASS + new window |
| `ENABLED` | Valid window, matching context, per-operation controls may be evaluated | Only after all existing gates and freshness checks pass | Continues until expiry, disarm, invalidation, PAUSED or LOCKED |
| `PAUSED` | Same window exists, but no trustworthy security verdict is obtainable because of pure reachability/availability failure | No | Automatic resume only after a trustworthy positive probe while the same window remains valid |
| `LOCKED` | Negative, ambiguous or untrusted security evidence invalidated the window | No | Full explicit rearm + new readiness PASS + new window |

Window expiry, explicit disarm and process restart return the system to `DISABLED`; a security-negative or ambiguous condition produces `LOCKED`.

## Relationship to ADR-0002

ADR-0004 does **not** change ADR-0002's session-key authorization criterion, bounded-capital risk acceptance, environment isolation requirements, `fund_movement_path_absent` premise or invalidation conditions.

ADR-0004 clarifies the runtime meaning of a readiness PASS derived from ADR-0002:

- the PASS/attestation is short-lived bootstrap evidence;
- it is not a self-renewing lease for a continuous worker;
- after one-shot consumption into an operational window, continuity is governed by the window's explicit operator intent, process/context binding, hard 24-hour ceiling and the unchanged per-operation controls.

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
- the arming mechanism changes from the explicit process-signal model;
- any proposal introduces persistent or remotely replayable authorization state;
- the readiness attestation TTL or issuance semantics change;
- the pre-POST freshness probe is moved, weakened or removed;
- gate 1, gate 2 or gate 4 changes materially;
- the RISEx signer/account/network/deployment identity model changes;
- ADR-0002 or ADR-0003 is invalidated or materially revised.

## Consequences

### Positive

- The 300-second readiness TTL keeps a precise purpose instead of becoming decorative.
- The worker cannot silently renew its own authorization.
- Restart and context changes are hard authorization boundaries.
- Human intent is re-established at least once every 24 hours without assuming an eight-hour staffed shift.
- Per-operation freshness and existing gates remain authoritative at the point of use.
- PAUSED and LOCKED have explicit fail-closed semantics.
- No PASS or execution authorization is persisted in Redis or the database.

### Negative / residual risks

- A live worker may remain authorized for up to 24 hours after a valid explicit arm, subject to all unchanged per-operation gates and freshness checks.
- The POSIX-signal mechanism depends on administrative access to the live Railway/Linux worker process and requires operational documentation for the operator.
- A 24-hour bound does not guarantee that a window never crosses a night; it guarantees only a maximum one-day authorization horizon.
- Process-local authorization means every deployment or restart requires a new explicit arm/readiness cycle.
- Incorrect classification of an error as reachability-only could delay LOCKED; the conservative rule therefore classifies malformed, ambiguous and authenticity-related failures as LOCKED.

## Alternatives considered

### Automatic periodic rearming

Rejected.

Even with a fixed maximum number of renewals, the worker would still be renewing its own authority. A limit such as twelve five-minute renewals merely moves the problem to sixty minutes and preserves the same conceptual flaw.

### Long or infinite readiness TTL

Rejected.

It collapses readiness evidence and continuous authorization into one durable fact and weakens the protection provided by a bounded readiness observation.

### Eight-hour operational window

Rejected as the default.

It assumes a staffed-shift model that the 24/7 worker does not have and would create repeated daily manual rearming pressure likely to encourage automation of the control.

### Seventy-two-hour operational window

Rejected.

It permits one explicit act to authorize multiple daily operating cycles and unattended periods, which is too weak a bound for the intended human-intent control.

### Persisted operator authorization in Redis or the database

Rejected.

Persistence would survive process restart and would convert a process-bound authorization into replayable shared state, contrary to the fail-closed restart boundary.

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

A separate TDD implementation PR is required.

Following the same lifecycle used for ADR-0003, this ADR remains **Proposed** in the documentation-only PR. The implementation PR may change the status to **Accepted** only in the same reviewed GREEN change that implements and verifies the decision. Acceptance must not be performed by this documentation-only PR.

## Related evidence and decisions

- `docs/adr/ADR-0002-risex-session-key-authorization-model.md`
- `docs/adr/ADR-0003-risex-fund-movement-negative-probe-applicability.md`
- `docs/superpowers/specs/2026-09-13-risex-signed-execution-orchestration-design.md`
- `backend/app/security/risex_signed_testnet_runner.py`
- `backend/app/services/risex_signed_execution.py`
- `backend/app/adapters/risex.py`
