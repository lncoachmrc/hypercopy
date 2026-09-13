# TRAXION RISEx Signed Execution Orchestration Design

**Date:** 2026-09-13
**Status:** approved; implementation authorized
**Baseline:** `main` at `f1ca502f686d09f4ae09ceb5b19c9c7e9eab0dac`

## Goal

Introduce one explicit, process-local orchestration layer that connects the existing RISEx signed-testnet readiness result to the existing signed transport and `RISExAdapter`, without changing normal CopyJob routing, Hyperliquid execution, persistence, Railway configuration or the signed-write opt-in flag.

PASSO 2E prepares an armed RISEx signed execution session for a deliberate controlled test flow. It does **not** route ordinary follower jobs to RISEx and does **not** enable signed writes by default.

## Current verified boundary

The prerequisites are already on `main`:

- `run_signed_testnet_readiness()` returns `RISExSignedTestnetReadinessResult` and emits a sealed `RISExRuntimeReadinessAttestation` only on ADR-0002 readiness PASS;
- `RISExAdapter.place_ioc()` consumes that attestation and requires the exact opt-in `RISEX_SIGNED_WRITES_ENABLED == 'true'`, an active RISEx testnet execution epoch, account/signer identity binding and the existing signed transport;
- `RISExSignedTestnetHTTPTransport.post_place_order()` performs the mandatory freshness probe immediately before the provider POST;
- `_process_job_locked()` remains Hyperliquid-only and rejects `execution_provider != 'hyperliquid'`.

PASSO 2E preserves all four facts.

## Critical evidence finding: pre-order gate cannot be honestly reconstructed from readiness

`authorize_pre_order_probe()` requires, among the deployment/session checks:

- `perps_permission is True`;
- `fund_movement_path_absent is True`;
- `fund_movement_rejected is True`;
- `withdrawal_rejected is True`;
- `operatorhub_bypass_disabled is True`;
- replay protection verified.

The public signer collector does **not** produce the two behavioral negative-test facts. `collect_public_signer_evidence()` returns `fund_movement_rejected=None` and `withdrawal_rejected=None`. The repository currently contains `True` values for those fields only in unit-test fixtures; no runtime producer has performed or recorded those negative tests against the provider.

### Decision

PASSO 2E must **not** convert `fund_movement_rejected` or `withdrawal_rejected` into operator assertions. Their semantics are behavioral: a concrete operation was attempted and RISEx rejected it. Setting them to true without an observed provider rejection would manufacture evidence.

`fund_movement_path_absent` is different by design: ADR-0002 defines it as an explicit reviewed architectural assertion, fail-closed by default and subject to a review date. That precedent does not extend to behavioral negative tests.

Therefore `arm_risex_signed_testnet_execution()` does **not** create the pre-order gate from readiness. It requires an already-attested `RISExPreOrderProbeGate` as an explicit dependency and verifies its seal before constructing a session. No weakening of `authorize_pre_order_probe()` belongs in PASSO 2E.

## New service

Create `backend/app/services/risex_signed_execution.py`.

Its only responsibility is to construct and own a short-lived, process-local RISEx signed execution session from already-approved security capabilities. It does not own CopyJob routing, database persistence, Railway configuration, credential storage, automatic retries or readiness-refresh scheduling.

## Public orchestration API

### `RISExSignedExecutionSession`

A narrow async context manager that owns one `RISExSignedTestnetHTTPTransport` and one `RISExAdapter` configured with the same runtime readiness attestation. It contains no persistent state and no refresh task.

```python
@dataclass(slots=True)
class RISExSignedExecutionSession:
    adapter: RISExAdapter
    _transport: RISExSignedTestnetHTTPTransport

    async def __aenter__(self) -> "RISExSignedExecutionSession": ...
    async def __aexit__(self, exc_type, exc, tb) -> None: ...
```

`__aexit__` always executes `await self._transport.aclose()`, including when `place_ioc()` or caller code raises. The session exposes the adapter rather than proxying `place_ioc()`, keeping the four reviewed gates in `RISExAdapter.place_ioc()` as the single write boundary.

### `arm_risex_signed_testnet_execution()`

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

The function is testnet-only. `transport_client` exists only for deterministic tests; the concrete transport still rejects unsafe ambient auth. Production callers omit it.

## Arm sequence

`arm_risex_signed_testnet_execution()` executes exactly this sequence:

1. validate the supplied `pre_order_gate` with `assert_pre_order_probe_gate_attested(pre_order_gate)`;
2. call `run_signed_testnet_readiness()` exactly once with `network='testnet'` and the explicit policy assertions supplied by the caller;
3. require `result.report.verdict == 'PASS'` and `result.attestation is not None`;
4. validate the runtime attestation with `assert_runtime_readiness_attested()` using the account and signer from the supplied pre-order gate and the injected clock;
5. construct one `RISExSignedTestnetHTTPTransport` with that gate and the supplied freshness probe;
6. construct one `RISExAdapter(network='testnet', transport=transport, readiness_attestation=result.attestation, readiness_clock=readiness_clock)`;
7. return `RISExSignedExecutionSession(adapter=adapter, _transport=transport)`.

If any step fails, no session is returned. If a transport has already been created when later construction fails, it is closed before re-raising.

## Security properties

### No automatic readiness refresh

One arm invokes `run_signed_testnet_readiness()` exactly once. The session stores the resulting attestation and has no timer, loop, callback, retry wrapper or expiry handler that reruns readiness.

After the fixed 300-second TTL expires, `RISExAdapter.place_ioc()` rejects through the existing cause-3 path. Re-arm is a separate deliberate caller action. The mandatory regression test counts runner invocations before and after expiry and requires the count to remain exactly one.

### Pre-order freshness remains separate

The orchestration layer verifies only that the supplied gate is structurally attested and identity-compatible with readiness. `RISExSignedTestnetHTTPTransport.post_place_order()` still executes its live freshness probe immediately before every POST. PASSO 2E does not modify that transport method.

### Signed-write flag remains off

PASSO 2E does not set, default, persist or otherwise mutate `RISEX_SIGNED_WRITES_ENABLED`. An armed session may exist while the flag is absent; in that state `place_ioc()` still fails at cause 1.

### No fabricated behavioral evidence

The service accepts no `fund_movement_rejected` or `withdrawal_rejected` booleans and constructs no `RISExSignerCapabilityEvidence`. A caller must supply a sealed pre-order gate obtained through the existing authorizer path.

### Process-local only

The session, runtime attestation and transport are never persisted to DB, Redis, files or JSON. Process restart destroys them and requires a deliberate re-arm.

## Lifecycle and error handling

The required usage is:

```python
async with await arm_risex_signed_testnet_execution(...) as session:
    await session.adapter.place_ioc(...)
```

Normal exit and exceptional exit both close the transport. No exception causes automatic readiness rerun, gate reconstruction, Hyperliquid fallback or Railway mutation.

## Files in implementation scope

Create only:

- `backend/app/services/risex_signed_execution.py`
- `backend/tests/unit/test_risex_signed_execution.py`

Do not modify `execution.py`, `execution_worker.py`, Hyperliquid files, `risex_authorization_session.py`, `risex_pre_order_gate.py`, `risex_signed_testnet_runner.py`, `risex.py`, `risex_signed_testnet_http.py`, database models/migrations, workflows/rulesets or Railway configuration. If an interface defect appears to require one of those changes, stop and redesign rather than expanding scope.

## TDD acceptance tests

The implementation PR starts with a RED-only commit. Tests must prove:

1. missing/forged pre-order gate fails before readiness;
2. readiness FAIL returns no session;
3. readiness PASS with no runtime attestation returns no session;
4. account mismatch vs gate fails;
5. signer mismatch vs gate fails;
6. valid prerequisites produce a session containing a `RISExAdapter`, the exact attestation and concrete signed testnet transport;
7. successful arm invokes readiness exactly once;
8. expired attestation rejects `place_ioc()` and the recorded readiness-runner call count remains exactly one;
9. pre-POST freshness still runs before any provider POST;
10. normal async-context exit closes transport;
11. exceptional async-context exit closes transport;
12. flag absent still blocks an otherwise armed session at cause 1;
13. orchestration never mutates `RISEX_SIGNED_WRITES_ENABLED`;
14. existing Hyperliquid-only `_process_job_locked()` behavior remains green without modifying its tests;
15. existing Hyperliquid suite remains green.

Tests 8 and 11 are mandatory regression invariants and may not be weakened or removed during review.

## Out of scope

CopyJob routing to RISEx, provider-neutral rewrite of execution, execution-worker RISEx adapter caching, persistence, automatic readiness renewal, TTL change, ADR-0002 changes, synthesis of behavioral evidence, enabling `RISEX_SIGNED_WRITES_ENABLED`, Railway changes/deploys, first real RISEx order, signer revocation test and mainnet.

## Operational blocker after PASSO 2E

Even after this service is implemented, the first real order remains blocked until a trustworthy path exists to supply a pre-order gate whose `fund_movement_rejected` and `withdrawal_rejected` requirements are backed by actual evidence rather than test fixtures.

There is an additional structural constraint: those two fields mean **“a concrete operation was attempted and the provider rejected it.”** ADR-0002 currently documents that the reviewed RISEx session-key model exposes no fund-movement operation that consumes a session-key signature: `withdraw` operates on `msg.sender`; `permitTransferFrom` requires an EIP-712 signature from the owner; and no reviewed fund-movement operation consumes the order-path `VerifyWitness`. A controlled negative probe may therefore have no legitimate session-key fund-movement endpoint to invoke at all. The absence of such an operation is different from observing a provider rejection.

PASSO 2E does not resolve that semantic mismatch. It records it as a known constraint on the next decision. A future step must first determine whether a genuine negative probe exists. If a real session-key fund-movement operation exists, the project can execute a controlled probe and record its rejection. If no such operation exists, the alternative is a separately approved ADR that reevaluates whether `fund_movement_rejected` and `withdrawal_rejected` remain meaningful prerequisites in light of the documented absence of a session-key fund-movement path. Neither route is selected or implemented in PASSO 2E.

## Acceptance criteria

PASSO 2E is complete when the orchestrator is explicit/process-local, one arm performs one readiness run, expiry never refreshes automatically, a sealed pre-order gate is mandatory, readiness identity matches that gate, pre-POST freshness remains mandatory, async cleanup works on success and exception, the signed-write flag remains untouched, ordinary CopyJob routing remains Hyperliquid-only, and all existing CI/security checks remain green.
