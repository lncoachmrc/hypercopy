# RISEx Signed Execution Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit process-local service that arms one short-lived RISEx signed-testnet execution session from existing sealed readiness and pre-order capabilities without changing CopyJob routing or enabling signed writes by default.

**Architecture:** `arm_risex_signed_testnet_execution()` runs readiness exactly once, requires an already-attested pre-order gate, proves gate/readiness identity agreement, constructs the existing signed HTTP transport, injects the resulting runtime attestation into `RISExAdapter`, and returns an async context-managed `RISExSignedExecutionSession`. The session owns transport cleanup and has no automatic refresh path.

**Tech Stack:** Python 3.12, asyncio, dataclasses, httpx, pytest/pytest-asyncio, existing TRAXION RISEx security modules.

**Spec:** `docs/superpowers/specs/2026-09-13-risex-signed-execution-orchestration-design.md`

## Global Constraints

- Implementation baseline must be current `main` containing PR #168; re-check SHA before branching.
- Create exactly one implementation PR for PASSO 2E.
- TDD is mandatory: one RED-only test commit must precede all production-code changes.
- `RISEX_SIGNED_WRITES_ENABLED` remains absent by default and must not be set in repository code or Railway.
- No modification to `backend/app/services/execution.py`, `backend/app/workers/execution_worker.py`, Hyperliquid files, database models/migrations, Railway configuration, workflow files or rulesets.
- No modification to `backend/app/security/risex_pre_order_gate.py`, `backend/app/security/risex_signed_testnet_runner.py`, `backend/app/adapters/risex.py`, or `backend/app/adapters/risex_signed_testnet_http.py` unless implementation is stopped and the design is re-approved.
- `fund_movement_rejected` and `withdrawal_rejected` are behavioral evidence, not operator assertions. PASSO 2E must not synthesize them.
- The existing `fund_movement_path_absent` explicit ADR-0002 assertion remains fail-closed with default `False`.
- One successful arm invokes `run_signed_testnet_readiness()` exactly once.
- Expiry never triggers automatic readiness renewal.
- `RISExSignedExecutionSession` must close its transport on both normal and exceptional async-context exit.
- The existing transport freshness probe remains mandatory immediately before provider POST.
- No Hyperliquid fallback, no mainnet, no first real order, no signer revocation action.
- No bypass, `--admin`, force push, skip/xfail, `continue-on-error`, ruleset/workflow changes or audit-threshold relaxation.

---

### Task 1: Create the RED orchestration contract

**Files:**
- Create: `backend/tests/unit/test_risex_signed_execution.py`
- Production files: none in this task

**Interfaces:**
- Consumes existing `RISExAdapter`, `RISExSignedTestnetHTTPTransport`, `RISExSignedTestnetReadinessResult`, `RISExPreOrderProbeGate`, `authorize_pre_order_probe()`, `run_signed_testnet_readiness()` and `ProviderWriteDisabled`.
- Produces the failing contract for future `RISExSignedExecutionSession` and `arm_risex_signed_testnet_execution()`.

- [ ] **Step 1: Create deterministic test helpers for clock, genuine readiness, gate, request, epoch and HTTP transport**

Use the existing patterns from `test_risex_place_ioc_signed_transport.py`; do not import helpers from another test module.

```python
ACCOUNT = '0x' + ('11' * 20)
SIGNER = '0x' + ('22' * 20)
OTHER_ACCOUNT = '0x' + ('33' * 20)
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

Create `_genuine_readiness_result(monkeypatch, clock)` by patching the runner's external collectors exactly as the current 2D tests do, then call the real `run_signed_testnet_readiness(..., network='testnet', fund_movement_path_absent=True, clock=clock)` and assert `result.attestation is not None`.

Create `_gate()` with `authorize_pre_order_probe()` using a unit-test `RISExSignerCapabilityEvidence` fixture where the existing required behavioral fields are `True`. Add a comment in the helper stating that these are **test fixtures only** and are not a runtime evidence source.

Create `_request()`, `_job()`, and an `httpx.AsyncClient` backed by `httpx.MockTransport` using the same typed request construction already covered by PR #168.

- [ ] **Step 2: Write RED test — missing or forged pre-order gate blocks arm before readiness**

```python
@pytest.mark.asyncio
@pytest.mark.parametrize('gate', [None, object()])
async def test_arm_requires_attested_pre_order_gate_before_readiness(monkeypatch, gate):
    readiness_calls = 0

    async def counted_readiness(**kwargs):
        nonlocal readiness_calls
        readiness_calls += 1
        raise AssertionError('readiness must not run for an invalid gate')

    monkeypatch.setattr(
        'app.services.risex_signed_execution.run_signed_testnet_readiness',
        counted_readiness,
    )

    with pytest.raises(SignedTestnetBlocked):
        await arm_risex_signed_testnet_execution(
            env={}, api=object(), rpc=object(), pre_order_gate=gate,
            freshness_probe=_fresh_probe,
            explicit_approval=True,
            disposable_account_asserted=True,
            dedicated_signer_asserted=True,
            operatorhub_bypass_disabled=True,
            fund_movement_path_absent=True,
        )

    assert readiness_calls == 0
```

- [ ] **Step 3: Write RED tests — readiness FAIL and PASS-without-attestation return no session**

Patch the orchestration module symbol `run_signed_testnet_readiness` with `AsyncMock` results:

```python
RISExSignedTestnetReadinessResult(
    report=replace(valid_result.report, verdict='FAIL'),
    attestation=None,
)
```

and:

```python
RISExSignedTestnetReadinessResult(
    report=replace(valid_result.report, verdict='PASS'),
    attestation=None,
)
```

Both tests must raise `SignedTestnetBlocked` and must not create a transport.

- [ ] **Step 4: Write RED tests — gate/readiness account and signer mismatches fail closed**

Use a genuine runner-issued attestation, then alter only the gate-side expected identity through separate genuine unit-test gates. Tests:

```python
async def test_arm_rejects_readiness_account_mismatch(...): ...
async def test_arm_rejects_readiness_signer_mismatch(...): ...
```

Expected: `SignedTestnetBlocked` from `assert_runtime_readiness_attested()` before a session is returned.

- [ ] **Step 5: Write RED test — successful arm invokes readiness exactly once and returns exact dependencies**

```python
@pytest.mark.asyncio
async def test_arm_runs_readiness_once_and_returns_process_local_session(...):
    genuine = await _genuine_readiness_result(monkeypatch, MutableClock(FIXED_NOW))
    runner = AsyncMock(return_value=genuine)
    monkeypatch.setattr(
        'app.services.risex_signed_execution.run_signed_testnet_readiness',
        runner,
    )

    session = await arm_risex_signed_testnet_execution(...)

    assert runner.await_count == 1
    assert isinstance(session, RISExSignedExecutionSession)
    assert isinstance(session.adapter, RISExAdapter)
    assert isinstance(session.adapter.transport, RISExSignedTestnetHTTPTransport)
    assert session.adapter.readiness_attestation is genuine.attestation
```

Also assert that no test or service setup adds `RISEX_SIGNED_WRITES_ENABLED` to `os.environ`.

- [ ] **Step 6: Write mandatory RED test — expiry never refreshes readiness, proven by call count**

This is the required anti-refresh invariant.

```python
@pytest.mark.asyncio
async def test_expired_session_never_refreshes_readiness_runner(monkeypatch):
    monkeypatch.setenv('RISEX_SIGNED_WRITES_ENABLED', 'true')
    clock = MutableClock(FIXED_NOW)
    genuine = await _genuine_readiness_result(monkeypatch, clock)
    runner = AsyncMock(return_value=genuine)
    monkeypatch.setattr(
        'app.services.risex_signed_execution.run_signed_testnet_readiness',
        runner,
    )

    session = await arm_risex_signed_testnet_execution(..., readiness_clock=clock)
    assert runner.await_count == 1

    clock.value = FIXED_NOW + 301
    await _allow_epoch(monkeypatch, matches=True)
    with pytest.raises(ProviderWriteDisabled, match='cause 3'):
        await session.adapter.place_ioc(db=object(), job=_job(), request=_request())

    assert runner.await_count == 1
```

The final assertion is mandatory and may not be replaced by a simple exception assertion.

- [ ] **Step 7: Write mandatory RED tests — async context closes transport on success and on exception**

Normal path:

```python
async with await arm_risex_signed_testnet_execution(...) as session:
    transport = session.adapter.transport
assert transport._client.is_closed
```

Exceptional path:

```python
transport = None
with pytest.raises(RuntimeError, match='boom'):
    async with await arm_risex_signed_testnet_execution(...) as session:
        transport = session.adapter.transport
        raise RuntimeError('boom')
assert transport is not None
assert transport._client.is_closed
```

The exception-path close test is mandatory and may not be removed in review.

- [ ] **Step 8: Write RED test — pre-POST freshness remains mandatory with an armed session**

Set the signed-write flag to `true` **only inside the unit test process**, patch the epoch fence to match, use a freshness probe that increments a counter and returns stale/revoked evidence, then call `session.adapter.place_ioc(...)`.

Expected:

```python
assert freshness_calls == 1
assert http_calls == []
with pytest.raises(ProviderWriteDisabled, match='cause 4'):
    ...
```

Do not modify the existing transport test or transport implementation.

- [ ] **Step 9: Write RED test — flag absent still blocks an otherwise armed session at cause 1**

```python
monkeypatch.delenv('RISEX_SIGNED_WRITES_ENABLED', raising=False)
session = await arm_risex_signed_testnet_execution(...)
with pytest.raises(ProviderWriteDisabled, match='cause 1'):
    await session.adapter.place_ioc(db=object(), job=_job(), request=_request())
```

Assert the readiness runner was called once and remains once. The orchestration layer may arm security state, but it must not enable writes.

- [ ] **Step 10: Run focused RED suite**

Run:

```bash
cd backend
pytest -q tests/unit/test_risex_signed_execution.py
```

Expected: failures caused by missing `app.services.risex_signed_execution`, `RISExSignedExecutionSession`, and `arm_risex_signed_testnet_execution`; no unrelated import/syntax failures.

- [ ] **Step 11: Run baseline non-regression suite on the RED branch**

Run:

```bash
ruff check .
pyright app
python -m compileall -q app
pytest -q
```

Expected: existing tests remain green; only the new 2E tests fail for the missing feature.

- [ ] **Step 12: Commit the RED state**

```bash
git add backend/tests/unit/test_risex_signed_execution.py
git commit -m "test: add RED coverage for RISEx signed execution orchestration"
```

Push the branch and capture the CI failure as TDD evidence before writing production code.

---

### Task 2: Implement the minimal GREEN orchestration service

**Files:**
- Create: `backend/app/services/risex_signed_execution.py`
- Test: `backend/tests/unit/test_risex_signed_execution.py`
- Do not modify any other production file.

**Interfaces:**
- Consumes:
  - `run_signed_testnet_readiness(...) -> RISExSignedTestnetReadinessResult`
  - `assert_runtime_readiness_attested(attestation, *, account_address, signer_address, clock)`
  - `assert_pre_order_probe_gate_attested(gate)`
  - `RISExSignedTestnetHTTPTransport(gate=..., client=..., freshness_probe=...)`
  - `RISExAdapter(network='testnet', transport=..., readiness_attestation=..., readiness_clock=...)`
- Produces:
  - `RISExSignedExecutionSession`
  - `arm_risex_signed_testnet_execution(...) -> RISExSignedExecutionSession`

- [ ] **Step 1: Implement the async session owner**

Create exactly this responsibility:

```python
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

Do not add refresh, retry, timer, scheduler, persistence or `place_ioc()` proxy methods.

- [ ] **Step 2: Implement the exact arm signature from the spec**

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

No `network` argument is exposed because PASSO 2E is testnet-only. Pass `network='testnet'` directly into the existing readiness runner and adapter construction.

- [ ] **Step 3: Validate the supplied pre-order gate before readiness**

```python
assert_pre_order_probe_gate_attested(pre_order_gate)
```

This must execute before calling the readiness runner. Do not construct `RISExSignerCapabilityEvidence` in this service.

- [ ] **Step 4: Execute readiness exactly once**

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

Do not wrap this in retry logic.

- [ ] **Step 5: Fail closed unless readiness returned a usable PASS capability**

```python
if result.report.verdict != 'PASS' or result.attestation is None:
    raise SignedTestnetBlocked(
        'RISEx signed execution cannot arm without a runtime readiness PASS attestation'
    )
```

- [ ] **Step 6: Bind readiness identity to the already-attested pre-order gate**

```python
assert_runtime_readiness_attested(
    result.attestation,
    account_address=pre_order_gate.account_address,
    signer_address=pre_order_gate.signer_address,
    clock=readiness_clock,
)
```

This validation is in addition to, not instead of, the later pre-POST freshness probe.

- [ ] **Step 7: Construct concrete transport and adapter; close transport on construction failure**

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

return RISExSignedExecutionSession(adapter=adapter, _transport=transport)
```

Do not inspect or mutate `RISEX_SIGNED_WRITES_ENABLED` here.

- [ ] **Step 8: Run focused tests**

```bash
cd backend
pytest -q tests/unit/test_risex_signed_execution.py
```

Expected: all 2E tests PASS.

- [ ] **Step 9: Run RISEx security/transport regressions**

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

- [ ] **Step 10: Run full backend verification**

```bash
ruff check .
pyright app
python -m compileall -q app
pytest -q
pip-audit
```

Expected: all green; no skipped/xfail additions.

- [ ] **Step 11: Commit GREEN**

```bash
git add backend/app/services/risex_signed_execution.py backend/tests/unit/test_risex_signed_execution.py
git commit -m "feat: add RISEx signed execution orchestrator"
```

Push without force.

---

### Task 3: PR verification and security review gate

**Files:**
- No additional source changes unless a review finding proves a PASSO 2E defect.

**Interfaces:**
- Consumes the RED and GREEN commits from Tasks 1-2.
- Produces one reviewable PASSO 2E PR with no merge authorization implied.

- [ ] **Step 1: Open one PR against current `main`**

Title:

```text
feat: add explicit RISEx signed execution orchestrator
```

PR body must state:

- RED commit SHA and failing-test evidence;
- GREEN commit SHA;
- one readiness run per arm;
- no automatic refresh after 300-second expiry;
- pre-order gate is supplied, not fabricated;
- behavioral negative-test evidence remains an operational blocker;
- transport closes on success and exception;
- `RISEX_SIGNED_WRITES_ENABLED` remains unset/untouched;
- no routing, Hyperliquid, DB, Railway, workflow or ruleset changes;
- merge not authorized.

- [ ] **Step 2: Verify changed files**

Expected production/test diff:

```text
backend/app/services/risex_signed_execution.py
backend/tests/unit/test_risex_signed_execution.py
```

The already-approved spec/plan docs may exist on their documentation branch; do not accidentally mix unrelated documentation-branch history into the implementation PR unless explicitly desired.

- [ ] **Step 3: Wait for all seven checks**

Required report:

```text
backend
frontend
landing
repository-tree
secrets
analyze (python)
analyze (javascript-typescript)
```

All must be SUCCESS before reporting completion.

- [ ] **Step 4: Triage every review thread**

For each thread, decide whether the PR introduced the defect or merely revealed it. If introduced, add a targeted RED regression before the fix. If revealed/out-of-scope, document it separately rather than expanding 2E silently.

Every thread must receive an argued response before resolution.

- [ ] **Step 5: Re-run final verification after review changes**

```bash
cd backend
ruff check .
pyright app
python -m compileall -q app
pytest -q
pip-audit
```

Then confirm the latest PR head has all seven GitHub checks green.

- [ ] **Step 6: Final report and stop**

Report:

- base and head SHA;
- RED and GREEN commit SHAs;
- exact files changed;
- focused and full test results;
- seven CI checks;
- review-thread count/resolution state;
- confirmation that `RISEX_SIGNED_WRITES_ENABLED` was not set;
- confirmation that no Railway deployment or variable mutation occurred;
- confirmation that `_process_job_locked()` still rejects non-Hyperliquid providers;
- explicit remaining blocker: trustworthy behavioral negative-test evidence for `fund_movement_rejected` and `withdrawal_rejected`.

Do not merge.
