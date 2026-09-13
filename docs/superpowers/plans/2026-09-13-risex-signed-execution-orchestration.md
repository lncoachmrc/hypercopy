# RISEx Signed Execution Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit process-local service that arms one short-lived RISEx signed-testnet execution session from existing sealed readiness and pre-order capabilities without changing CopyJob routing or enabling signed writes by default.

**Architecture:** `arm_risex_signed_testnet_execution()` runs readiness exactly once, requires an already-attested pre-order gate, proves gate/readiness identity agreement, constructs the existing signed HTTP transport, injects the resulting runtime attestation into `RISExAdapter`, and returns an async context-managed `RISExSignedExecutionSession`. The session owns transport cleanup and has no automatic refresh path.

**Tech Stack:** Python 3.12, asyncio, dataclasses, httpx, pytest/pytest-asyncio, existing TRAXION RISEx security modules.

**Spec:** `docs/superpowers/specs/2026-09-13-risex-signed-execution-orchestration-design.md`

## Global Constraints

- Re-check `main` before branching; it must contain PR #168.
- Create exactly one implementation PR for PASSO 2E.
- TDD is mandatory: a RED-only test commit must precede all production-code changes.
- `RISEX_SIGNED_WRITES_ENABLED` remains absent by default and must not be set in repository code or Railway.
- Do not modify `backend/app/services/execution.py`, `backend/app/workers/execution_worker.py`, Hyperliquid files, database models/migrations, Railway configuration, workflow files or rulesets.
- Do not modify `backend/app/security/risex_pre_order_gate.py`, `backend/app/security/risex_signed_testnet_runner.py`, `backend/app/adapters/risex.py`, or `backend/app/adapters/risex_signed_testnet_http.py`. If implementation proves one of those interfaces insufficient, stop and re-design.
- `fund_movement_rejected` and `withdrawal_rejected` remain behavioral evidence. PASSO 2E must not synthesize or relabel them as assertions.
- `fund_movement_path_absent` remains the existing explicit ADR-0002 assertion with default `False`.
- One successful arm invokes `run_signed_testnet_readiness()` exactly once.
- Expiry never triggers automatic readiness renewal.
- `RISExSignedExecutionSession` closes its transport on normal and exceptional context exit.
- The existing transport freshness probe remains mandatory immediately before provider POST.
- No Hyperliquid fallback, mainnet action, first real order or signer revocation action.
- No bypass, `--admin`, force push, skip/xfail, `continue-on-error`, ruleset/workflow changes or audit-threshold relaxation.

---

### Task 1: RED-only orchestration contract

**Files:**
- Create: `backend/tests/unit/test_risex_signed_execution.py`
- Production files: none

**Interfaces:**
- Consumes existing `RISExAdapter`, `RISExSignedTestnetHTTPTransport`, `RISExSignedTestnetReadinessResult`, `RISExPreOrderProbeGate`, `authorize_pre_order_probe()`, `run_signed_testnet_readiness()`, `ProviderWriteDisabled`, `SignedTestnetBlocked`.
- Produces the failing contract for `RISExSignedExecutionSession` and `arm_risex_signed_testnet_execution()`.

- [ ] **Step 1: Recreate deterministic local test helpers in the new test module**

Use the concrete construction already exercised by `backend/tests/unit/test_risex_place_ioc_signed_transport.py`:

```python
ACCOUNT = '0x' + ('11' * 20)
SIGNER = '0x' + ('22' * 20)
OTHER_ACCOUNT = '0x' + ('33' * 20)
OTHER_SIGNER = '0x' + ('44' * 20)
AUTH = '0x' + ('aa' * 20)
ROUTER = '0x' + ('bb' * 20)
CHAIN_ID = 11155931
SESSION_EXPIRATION = 4_000_000_000
FIXED_NOW = 1_900_000_000.0

class MutableClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value
```

The module must define these helpers with complete bodies, using the same concrete types as the existing 2D tests:

- `_genuine_readiness_result(monkeypatch, clock) -> RISExSignedTestnetReadinessResult`: patch the runner's external deployment/session collectors, call the **real** `run_signed_testnet_readiness()` with `network='testnet'`, all four policy assertions `True`, `fund_movement_path_absent=True`, deterministic clock, and assert a non-null attestation.
- `_capability_evidence(account=ACCOUNT, signer=SIGNER) -> RISExSignerCapabilityEvidence`: set test-only `fund_movement_rejected=True` and `withdrawal_rejected=True`; include an inline comment that these are unit-test fixtures, not runtime evidence.
- `_gate(account=ACCOUNT, signer=SIGNER) -> RISExPreOrderProbeGate`: call the real `authorize_pre_order_probe()` with a testnet PASS policy and `replay_protection_verified=True`.
- `_request(account=ACCOUNT, signer=SIGNER) -> RISExPreparedPlaceOrderRequest`: construct a real `RISExPlaceOrder`, real `RISExPreparedPlaceOrderPermit`, then call `prepare_place_order_request()`.
- `_job() -> SimpleNamespace`: return a RISEx/testnet job with non-null `execution_epoch_id`.
- `_allow_epoch(monkeypatch)`: patch `app.adapters.risex.job_matches_active_destination` to return `True`.
- `_fresh_probe() -> RISExSignerCapabilityEvidence`: return matching active testnet evidence.
- `_stale_probe() -> RISExSignerCapabilityEvidence`: return matching evidence with `session_active=False`.

- [ ] **Step 2: Write all RED tests before production code**

Create exactly these tests and assertions:

1. `test_arm_rejects_missing_gate_before_readiness` — pass `None`, expect `SignedTestnetBlocked`; patched readiness runner call count remains `0`.
2. `test_arm_rejects_forged_gate_before_readiness` — pass `object()`, expect `SignedTestnetBlocked`; readiness call count remains `0`.
3. `test_arm_rejects_readiness_fail` — patched readiness returns a report with `verdict='FAIL'` and `attestation=None`; expect `SignedTestnetBlocked`.
4. `test_arm_rejects_pass_without_attestation` — patched readiness returns `verdict='PASS'` and `attestation=None`; expect `SignedTestnetBlocked`.
5. `test_arm_rejects_readiness_account_mismatch` — genuine attestation account differs from genuine gate account; expect `SignedTestnetBlocked` before transport construction.
6. `test_arm_rejects_readiness_signer_mismatch` — genuine attestation signer differs from genuine gate signer; expect `SignedTestnetBlocked` before transport construction.
7. `test_arm_runs_readiness_once_and_returns_session` — patched runner returns a genuine result; assert `await_count == 1`, session type is `RISExSignedExecutionSession`, adapter type is `RISExAdapter`, transport type is `RISExSignedTestnetHTTPTransport`, and adapter holds the exact attestation object.
8. `test_expired_session_never_refreshes_readiness_runner` — set test-process flag to exact `true`, arm once, assert runner count `1`, advance injected clock to `FIXED_NOW + 301`, call `place_ioc()`, expect `ProviderWriteDisabled` cause 3, assert runner count still exactly `1`.
9. `test_armed_session_keeps_pre_post_freshness_probe_mandatory` — set test-process flag to exact `true`, match epoch, use stale freshness probe and MockTransport, expect cause 4, freshness count `1`, HTTP POST count `0`.
10. `test_context_exit_closes_transport_normally` — enter and exit async context without error, assert the injected `httpx.AsyncClient.is_closed` is `True` after exit.
11. `test_context_exit_closes_transport_when_body_raises` — raise `RuntimeError('boom')` inside async context, assert the error propagates and client is closed afterward.
12. `test_flag_absent_still_blocks_armed_session` — explicitly delete test-process `RISEX_SIGNED_WRITES_ENABLED`, arm successfully, call `place_ioc()`, expect cause 1 and runner count exactly `1`.
13. `test_arm_does_not_mutate_signed_write_flag` — snapshot `os.environ.get('RISEX_SIGNED_WRITES_ENABLED')`, arm, assert the value is unchanged.

- [ ] **Step 3: Run the focused RED suite**

```bash
cd backend
pytest -q tests/unit/test_risex_signed_execution.py
```

Expected: the new tests fail because `app.services.risex_signed_execution`, `RISExSignedExecutionSession`, and `arm_risex_signed_testnet_execution()` do not yet exist. Import/syntax errors inside the test file itself are not an acceptable RED state and must be fixed before committing.

- [ ] **Step 4: Run the baseline suite to attribute failures correctly**

```bash
ruff check .
pyright app
python -m compileall -q app
pytest -q --ignore=tests/unit/test_risex_signed_execution.py
```

Expected: existing backend checks/tests are green. This proves the RED failures are introduced only by the new contract.

- [ ] **Step 5: Commit and push RED**

```bash
git add backend/tests/unit/test_risex_signed_execution.py
git commit -m "test: add RED coverage for RISEx signed execution orchestration"
git push -u origin HEAD
```

Capture the focused failure count and GitHub backend failure as TDD evidence. Do not add production code until RED is verified for the intended reason.

---

### Task 2: Minimal GREEN service

**Files:**
- Create: `backend/app/services/risex_signed_execution.py`
- Test: `backend/tests/unit/test_risex_signed_execution.py`
- No other production file changes.

**Interfaces:**
- Consumes `run_signed_testnet_readiness`, `assert_runtime_readiness_attested`, `assert_pre_order_probe_gate_attested`, `RISExSignedTestnetHTTPTransport`, `RISExAdapter`.
- Produces `RISExSignedExecutionSession` and `arm_risex_signed_testnet_execution()`.

- [ ] **Step 1: Add imports and the session owner**

Implement the class exactly:

```python
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from time import time as _system_clock

import httpx

from app.adapters.risex import RISExAdapter
from app.adapters.risex_signed_testnet_http import (
    RISExPreOrderFreshnessProbe,
    RISExSignedTestnetHTTPTransport,
)
from app.security.risex_deployment_runtime import PublicAPITransport, PublicRPCTransport
from app.security.risex_pre_order_gate import (
    RISExPreOrderProbeGate,
    assert_pre_order_probe_gate_attested,
)
from app.security.risex_signed_testnet_policy import SignedTestnetBlocked
from app.security.risex_signed_testnet_runner import (
    RuntimeClock,
    assert_runtime_readiness_attested,
    run_signed_testnet_readiness,
)


@dataclass(slots=True)
class RISExSignedExecutionSession:
    adapter: RISExAdapter
    _transport: RISExSignedTestnetHTTPTransport

    async def __aenter__(self) -> 'RISExSignedExecutionSession':
        return self

    async def __aexit__(
        self,
        _exc_type: object,
        _exc: object,
        _tb: object,
    ) -> None:
        await self._transport.aclose()
```

- [ ] **Step 2: Implement the exact arm signature**

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
```

Do not add a `network` parameter. PASSO 2E hard-binds the arm path to testnet.

- [ ] **Step 3: Validate the supplied gate before running readiness**

```python
assert_pre_order_probe_gate_attested(pre_order_gate)
```

Do not create `RISExSignerCapabilityEvidence` or call `authorize_pre_order_probe()` in this module.

- [ ] **Step 4: Run readiness exactly once**

```python
result = await run_signed_testnet_readiness(
    env=env,
    api=api,
    rpc=rpc,
    network='testnet',
    explicit_approval=explicit_approval,
    disposable_account_asserted=disposable_account_asserted,
    dedicated_signer_asserted=dedicated_signer_asserted,
    operatorhub_bypass_disabled=operatorhub_bypass_disabled,
    fund_movement_path_absent=fund_movement_path_absent,
    clock=readiness_clock,
)
```

No retry or refresh wrapper is permitted.

- [ ] **Step 5: Require a PASS attestation and bind identities**

```python
if result.report.verdict != 'PASS' or result.attestation is None:
    raise SignedTestnetBlocked(
        'RISEx signed execution cannot arm without a runtime readiness PASS attestation'
    )

assert_runtime_readiness_attested(
    result.attestation,
    account_address=pre_order_gate.account_address,
    signer_address=pre_order_gate.signer_address,
    clock=readiness_clock,
)
```

- [ ] **Step 6: Construct the concrete transport and adapter**

```python
transport = RISExSignedTestnetHTTPTransport(
    gate=pre_order_gate,
    client=transport_client,
    freshness_probe=freshness_probe,
)
try:
    adapter = RISExAdapter(
        network='testnet',
        transport=transport,
        readiness_attestation=result.attestation,
        readiness_clock=readiness_clock,
    )
except BaseException:
    await transport.aclose()
    raise

return RISExSignedExecutionSession(
    adapter=adapter,
    _transport=transport,
)
```

The module must not inspect or mutate `RISEX_SIGNED_WRITES_ENABLED`.

- [ ] **Step 7: Run focused 2E tests**

```bash
cd backend
pytest -q tests/unit/test_risex_signed_execution.py
```

Expected: all 13 tests PASS.

- [ ] **Step 8: Run existing RISEx regressions unchanged**

```bash
pytest -q \
  tests/unit/test_risex_runtime_readiness_attestation.py \
  tests/unit/test_risex_runtime_readiness_attestation_review.py \
  tests/unit/test_risex_pre_order_gate.py \
  tests/unit/test_risex_signed_testnet_http.py \
  tests/unit/test_risex_signed_testnet_typed_transport.py \
  tests/unit/test_risex_place_ioc_signed_transport.py \
  tests/unit/test_risex_place_ioc_signed_transport_review.py
```

Expected: PASS without modifying those files.

- [ ] **Step 9: Run full backend verification**

```bash
ruff check .
pyright app
python -m compileall -q app
pytest -q
pip-audit
```

Expected: all green; no new skip/xfail markers.

- [ ] **Step 10: Commit and push GREEN**

```bash
git add backend/app/services/risex_signed_execution.py backend/tests/unit/test_risex_signed_execution.py
git commit -m "feat: add RISEx signed execution orchestrator"
git push
```

---

### Task 3: One PR, CI and review closure

**Files:**
- No source changes unless a review finding identifies a defect introduced by PASSO 2E.

**Interfaces:**
- Consumes the verified RED and GREEN commits.
- Produces one reviewable, unmerged PASSO 2E PR.

- [ ] **Step 1: Open one PR against current `main`**

Use title:

```text
feat: add explicit RISEx signed execution orchestrator
```

The PR body must record the RED SHA/failure count, GREEN SHA, one readiness call per arm, no refresh after expiry, mandatory pre-existing gate, behavioral-evidence blocker, context-manager cleanup on exception, flag untouched, scope exclusions, and explicit `NON MERGIARE` status.

- [ ] **Step 2: Verify the implementation diff is limited to two files**

Expected implementation files:

```text
backend/app/services/risex_signed_execution.py
backend/tests/unit/test_risex_signed_execution.py
```

If any other production file appears, stop and explain why before proceeding.

- [ ] **Step 3: Wait for the seven required checks**

Report these individually:

```text
backend
frontend
landing
repository-tree
secrets
analyze (python)
analyze (javascript-typescript)
```

All seven must be SUCCESS on the final head.

- [ ] **Step 4: Triage every review thread**

For each thread: determine whether the PR introduced the defect. An introduced defect requires a new targeted RED test before the fix. A revealed out-of-scope defect must be documented separately instead of expanding 2E silently. Reply with technical evidence before resolving each thread.

- [ ] **Step 5: Re-run final backend verification after review changes**

```bash
cd backend
ruff check .
pyright app
python -m compileall -q app
pytest -q
pip-audit
```

Then confirm the latest PR head still has all seven GitHub checks green.

- [ ] **Step 6: Final report and stop without merge**

Report base/head SHA, RED/GREEN SHAs, changed files, focused/full test counts, seven CI checks, review-thread state, confirmation that `RISEX_SIGNED_WRITES_ENABLED` was not set, no Railway mutation/deploy, and confirmation that `_process_job_locked()` remains Hyperliquid-only.

Repeat the remaining operational blocker verbatim in substance: a trustworthy producer for `fund_movement_rejected=True` and `withdrawal_rejected=True` does not yet exist, so a real pre-order gate must not be fabricated from test fixtures.
