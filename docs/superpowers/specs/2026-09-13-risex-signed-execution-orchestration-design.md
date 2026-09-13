# TRAXION RISEx Signed Execution Orchestration Design

**Date:** 2026-09-13
**Status:** approved design boundary; implementation not yet authorized
**Baseline:** `main` at `f1ca502f686d09f4ae09ceb5b19c9c7e9eab0dac`

## Goal

Introduce one explicit, process-local orchestration layer that connects the existing RISEx signed-testnet readiness result to the existing signed transport and `RISExAdapter` without changing normal CopyJob routing, Hyperliquid execution, persistence, Railway configuration or the signed-write opt-in flag.

This is PASSO 2E. It prepares an armed RISEx signed execution session for a deliberate controlled test flow. It does **not** route ordinary follower jobs to RISEx and does **not** enable signed writes by default.

## Current verified boundary

The prerequisites are already on `main`:

- `run_signed_testnet_readiness()` returns `RISExSignedTestnetReadinessResult` with a sealed `RISExRuntimeReadinessAttestation` only on ADR-0002 readiness PASS.
- `RISExAdapter.place_ioc()` consumes that attestation and requires the exact opt-in `RISEX_SIGNED_WRITES_ENABLED == 'true'`, an active RISEx testnet execution epoch, account/signer identity binding, and the existing signed transport.
- `RISExSignedTestnetHTTPTransport.post_place_order()` performs the mandatory freshness probe immediately before the provider POST.
- `_process_job_locked()` remains Hyperliquid-only and rejects `execution_provider != 'hyperliquid'`.

PASSO 2E must preserve all four facts.

## Critical evidence finding: pre-order gate cannot be honestly reconstructed from readiness

`authorize_pre_order_probe()` currently requires all of the following positive facts before it can issue a `RISExPreOrderProbeGate`:

- active testnet signer/session and deployment identity;
- `perps_permission is True`;
- `fund_movement_path_absent is True`;
- `fund_movement_rejected is True`;
- `withdrawal_rejected is True`;
- `operatorhub_bypass_disabled is True`;
- replay protection verified.

The current public signer collector does **not** produce the two behavioral negative-test facts. `collect_public_signer_evidence()` explicitly returns:

- `fund_movement_rejected=None`;
- `withdrawal_rejected=None`.

The repository currently contains `True` values for those fields only in unit-test fixtures. No runtime producer has performed or recorded those negative tests against the provider.

### Decision

PASSO 2E must **not** convert `fund_movement_rejected` or `withdrawal_rejected` into operator assertions. Their existing semantics describe observed behavioral rejection by RISEx authorization; asserting them without an executed test would manufacture evidence.

`fund_movement_path_absent` remains different by design: ADR-0002 already defines it as an explicit reviewed architectural assertion, fail-closed by default. That precedent does not extend to behavioral negative tests.

Therefore `arm_risex_signed_testnet_execution()` does **not** create the pre-order gate from readiness. It requires an already-attested `RISExPreOrderProbeGate` as an explicit dependency and verifies its seal before constructing the session.

This makes the operational blocker visible instead of hiding it: until a separate controlled process supplies genuine evidence for the required negative tests and calls `authorize_pre_order_probe()`, a real signed execution session cannot be armed honestly.

No weakening of `authorize_pre_order_probe()` belongs in PASSO 2E.

## New service

Create:

`backend/app/services/risex_signed_execution.py`

The module has one responsibility: construct and own a short-lived, process-local RISEx signed execution session from already-approved security capabilities.

It must not own CopyJob routing, database persistence, Railway configuration, credential storage, automatic retries or readiness refresh scheduling.

## Public orchestration API

### `RISExSignedExecutionSession`

A narrow async context manager that owns:

- one `RISExSignedTestnetHTTPTransport`;
- one `RISExAdapter` configured with the same runtime readiness attestation;
- no persistent state and no refresh task.

Proposed shape:

```python
@dataclass(slots=True)
class RISExSignedExecutionSession:
    adapter: RISExAdapter
    _transport: RISExSignedTestnetHTTPTransport

    async def __aenter__(self) -> "RISExSignedExecutionSession": ...
    async def __aexit__(self, exc_type, exc, tb) -> None: ...
```

`__aexit__` always calls `await self._transport.aclose()`, including when `place_ioc()` or any caller code raises. The transport lifecycle belongs to the session.

The session exposes the adapter rather than proxying `place_ioc()`. This keeps the already-reviewed four gates in `RISExAdapter.place_ioc()` as the single write boundary.

### `arm_risex_signed_testnet_execution()`

Proposed signature:

```python
async def arm_risex_signed_testnet_execution(
    *,
    env: Mapping[str, str],
    api: PublicAPITransport,
    rpc: PublicRPCTransport,
    pre_order_gate: RISExPreOrderProbeGate,
    freshness_probe: RISExPreOrderFreshnessProbe,
    explicit_approval: bool,
    disposable_account_asserted: bool,
    dedicated_signer_asserted: bool,
    operatorhub_bypass_disabled: bool,
    fund_movement_path_absent: bool = False,
    readiness_clock: RuntimeClock = _system_clock,
    transport_client: httpx.AsyncClient | None = None,
) -> RISExSignedExecutionSession:
    ...
```

`transport_client` exists only to allow deterministic unit/integration transport tests. The concrete `RISExSignedTestnetHTTPTransport` still rejects ambient auth, cookies, request hooks and unsafe client state. Production callers omit it.

The function is testnet-only. It never accepts mainnet as an orchestration target.

## Arm sequence

`arm_risex_signed_testnet_execution()` executes exactly this sequence:

1. Structurally validate the supplied `pre_order_gate` with `assert_pre_order_probe_gate_attested(pre_order_gate)`.
2. Call `run_signed_testnet_readiness()` exactly once with `network='testnet'` and the explicit policy assertions supplied by the caller.
3. Require `result.report.verdict == 'PASS'` and `result.attestation is not None`.
4. Validate the runtime attestation immediately with `assert_runtime_readiness_attested()` using the account and signer from the supplied pre-order gate and the injected clock. This proves that readiness and pre-order gate identities refer to the same account/signer before the session exists.
5. Construct one `RISExSignedTestnetHTTPTransport` using the supplied pre-order gate and freshness probe.
6. Construct one `RISExAdapter(network='testnet', transport=transport, readiness_attestation=result.attestation, readiness_clock=readiness_clock)`.
7. Return `RISExSignedExecutionSession(adapter=adapter, _transport=transport)`.

If any step fails, no armed session is returned. If the transport has already been created when a later construction step fails, it must be closed before re-raising.

## Security properties

### No automatic readiness refresh

One call to `arm_risex_signed_testnet_execution()` invokes `run_signed_testnet_readiness()` exactly once.

The session stores only the resulting attestation. It has no timer, loop, callback, retry wrapper or expiry handler that reruns readiness.

When the 300-second runtime attestation expires, `RISExAdapter.place_ioc()` rejects it through the existing cause-3 path. The session remains stale until the caller deliberately leaves/discards it and explicitly calls `arm_risex_signed_testnet_execution()` again.

The mandatory regression test must count invocations of `run_signed_testnet_readiness()` before and after expiry. Merely observing a rejected order is insufficient proof that no refresh occurred.

### Pre-order gate freshness remains separate

The orchestration layer only verifies that the supplied pre-order gate is structurally attested and identity-compatible with readiness.

It does not replace the existing live revalidation. `RISExSignedTestnetHTTPTransport.post_place_order()` still invokes the supplied freshness probe immediately before every POST and calls `assert_pre_order_probe_gate_attested(..., now=..., evidence=...)`.

PASSO 2E must not modify that transport method.

### Signed-write flag remains off

PASSO 2E does not set, read-modify, default or persist `RISEX_SIGNED_WRITES_ENABLED`.

An armed session can exist while the environment variable is absent. In that state `session.adapter.place_ioc()` still fails at existing cause 1. Enabling the flag is a separate deliberate operational action after code review and test-environment verification.

### No fabricated behavioral evidence

The orchestration service never accepts booleans named `fund_movement_rejected` or `withdrawal_rejected` and never constructs `RISExSignerCapabilityEvidence` on behalf of the operator.

A caller must supply a sealed pre-order gate obtained through the existing gate-authorizer path. The unresolved provenance of the behavioral negative-test facts remains a visible prerequisite for the first real order.

### Process-local only

The session, runtime attestation and transport are never persisted to database, Redis, files or JSON.

Process restart destroys the armed session and requires deliberate re-arm.

## Lifecycle and error handling

The context-manager contract is mandatory:

```python
async with await arm_risex_signed_testnet_execution(...) as session:
    await session.adapter.place_ioc(...)
```

Whether the body returns normally or raises, `RISExSignedExecutionSession.__aexit__()` closes the transport.

If `aclose()` itself fails while another exception is already active, normal Python context-manager semantics apply: the close failure is allowed to propagate/chains naturally; the session must not silently swallow transport-close errors.

No exception causes automatic readiness rerun, gate reconstruction, Hyperliquid fallback or Railway mutation.

## Files in scope for implementation

Create:

- `backend/app/services/risex_signed_execution.py`
- `backend/tests/unit/test_risex_signed_execution.py`

Existing security/adapter modules are dependencies and should remain unchanged unless a test proves an unavoidable interface defect. In particular, implementation must not modify:

- `backend/app/services/execution.py`
- `backend/app/workers/execution_worker.py`
- Hyperliquid adapters/services
- `backend/app/security/risex_authorization_session.py`
- `backend/app/security/risex_pre_order_gate.py`
- `backend/app/security/risex_signed_testnet_runner.py`
- `backend/app/adapters/risex.py`
- `backend/app/adapters/risex_signed_testnet_http.py`
- database models/migrations
- workflow/ruleset files
- Railway configuration

If implementation discovers that one of these files must change, stop and re-design rather than expanding scope silently.

## TDD acceptance tests

The implementation PR must begin with a RED-only commit containing orchestration tests. At minimum it proves:

1. missing/forged `RISExPreOrderProbeGate` fails before session construction;
2. readiness FAIL returns no session;
3. readiness PASS with no runtime attestation returns no session;
4. readiness attestation account mismatch vs pre-order gate fails;
5. readiness attestation signer mismatch vs pre-order gate fails;
6. valid prerequisites produce one session containing a `RISExAdapter` with the exact attestation and concrete signed testnet transport;
7. `run_signed_testnet_readiness()` is invoked exactly once per successful arm;
8. after the attestation expires, an attempted `place_ioc()` is rejected and the recorded readiness-runner call count remains exactly one;
9. the existing pre-POST freshness probe is still invoked with a valid session before the transport attempts a POST;
10. async context exit closes the transport on the normal path;
11. async context exit closes the transport when `place_ioc()` or caller code raises;
12. with `RISEX_SIGNED_WRITES_ENABLED` absent, an otherwise armed session still fails at adapter cause 1;
13. no call path in the orchestration service mutates `RISEX_SIGNED_WRITES_ENABLED`;
14. existing `_process_job_locked()` Hyperliquid-only behavior remains green without modifying its tests;
15. existing Hyperliquid test suite remains green.

Tests 8 and 11 are mandatory regression invariants and may not be weakened or removed during review.

## Out of scope

- routing `CopyJob` to RISEx;
- provider-neutral rewrite of `execution.py`;
- execution-worker construction/cache of RISEx adapters;
- persistence of armed sessions or attestations;
- automatic readiness renewal;
- changing the 300-second TTL;
- changing ADR-0002 or `authorize_pre_order_probe()` requirements;
- synthesizing behavioral negative-test evidence;
- enabling `RISEX_SIGNED_WRITES_ENABLED`;
- any Railway deploy or variable mutation;
- first real RISEx order;
- signer revocation test;
- mainnet.

## Operational blocker after PASSO 2E

Even after this service is implemented, the first real order remains blocked until a trustworthy runtime path exists to supply a pre-order gate whose `fund_movement_rejected` and `withdrawal_rejected` requirements are backed by actual evidence rather than test fixtures.

That blocker must be resolved in a separate reviewed step. Possible future solutions include a controlled negative-probe procedure that records the provider responses and then authorizes the gate, or a separately approved ADR change if the project decides those behavioral checks are redundant under ADR-0002. PASSO 2E chooses neither.

## Acceptance criteria

PASSO 2E is complete when:

- the new orchestration service is process-local and explicit;
- one arm performs exactly one readiness run;
- no expiry path refreshes readiness automatically;
- a pre-existing sealed pre-order gate is mandatory;
- readiness and gate account/signer identities must match;
- the concrete signed transport keeps its existing pre-POST freshness probe;
- the async session closes the transport on success and exception;
- the signed-write flag remains absent/untouched;
- normal CopyJob routing remains Hyperliquid-only;
- no production/Railway/mainnet change occurs;
- all existing CI and security checks remain green.
