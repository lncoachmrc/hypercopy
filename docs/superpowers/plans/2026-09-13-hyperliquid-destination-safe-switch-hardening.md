# Hyperliquid Destination Safe-Switch Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the existing Hyperliquid network-switch path so provider/network destination changes are authorized centrally by `set_user_destination()` using fail-closed local and provider-side evidence before any destructive cleanup.

**Architecture:** Introduce a focused destination-switch assessment service that classifies the source as `VERIFIED_FLAT`, `NEVER_ACTIVATED`, or `UNREADABLE`. `set_user_destination()` remains the authoritative mutation boundary and invokes that service itself whenever persisted provider or network changes. `PUT /trading-network` becomes a thin caller: it may expose local readiness, but it performs no destructive cleanup until the central transition succeeds.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async/PostgreSQL, Hyperliquid Python SDK, Redis-backed rate limiter, pytest, Ruff, Pyright.

**Spec:** `docs/superpowers/specs/2026-09-13-risex-provider-selection-safe-switch-design.md`

## Global Constraints

- Backend only. Do not add `/trading-provider` and do not touch frontend files.
- Hyperliquid remains the default provider.
- Every provider/network switch creates a new epoch; old epoch rows remain immutable except `ended_at`.
- `set_user_destination()` is the authoritative switch boundary. Endpoint-only authorization is insufficient.
- `NEVER_ACTIVATED` is database-proven lifecycle state, never a fallback for provider read failure.
- Any provider read failure, malformed payload, unsupported activated-provider read, or identity mismatch is `UNREADABLE` and blocks.
- Existing Hyperliquid account-link/rebind behavior must remain supported and must not be mistaken for provider/network switch.
- No RISEx order, no execution-worker routing change, no Railway change, no mainnet deployment.
- RED before GREEN for every behavior change. No skip/xfail, bypass, force push or workflow/ruleset modification.

---

## File Structure

### Create

- `backend/app/services/destination_switch.py`
  - owns DB-local blockers, lifecycle classification and provider-side read verification;
  - exposes typed assessment/blocker results;
  - creates the concrete Hyperliquid read adapter internally for production calls;
  - never performs provider writes or epoch mutation.
- `backend/tests/unit/test_destination_switch.py`
  - pure/unit coverage for classification and provider evidence.
- `backend/tests/integration/test_destination_switch_hardening.py`
  - PostgreSQL coverage for central-boundary behavior, cleanup ordering, concurrency and rollback.

### Modify

- `backend/app/services/execution_destination.py`
  - detect genuine provider/network switch from persisted user identity;
  - invoke central assessment before closing/opening epoch;
  - preserve initial materialization, exact no-op and same-provider credential/account rebind semantics.
- `backend/app/adapters/hyperliquid.py`
  - add one read-only wrapper for `frontendOpenOrders` using the existing `_read()` limiter/retry path.
- `backend/app/api/user.py`
  - reuse shared DB-local blocker presentation;
  - reorder `/trading-network` so transition authorization occurs before TradingAccount/ledger/RiskState cleanup.
- `backend/tests/unit/test_hyperliquid_helpers.py`
  - read-wrapper behavior and malformed/read-error propagation.
- `backend/tests/unit/test_user_network.py`
  - local blocker compatibility and current API presentation.
- `backend/tests/integration/test_trading_account_epoch_lifecycle.py`
  - same-provider account/credential rebind non-regression.
- `backend/tests/integration/test_destination_epoch_fence.py`
  - adapt stale-epoch regression so no pending job is used to authorize a switch.

---

### Task 1: RED — model the three source states and DB-local blockers

**Files:**
- Create: `backend/tests/unit/test_destination_switch.py`
- Create later in GREEN: `backend/app/services/destination_switch.py`

**Interfaces:**
- Produces:
  - `DestinationLifecycleState = Literal['VERIFIED_FLAT', 'NEVER_ACTIVATED', 'UNREADABLE']` or equivalent enum.
  - `DestinationSwitchBlocker(code: str, message: str)`.
  - `DestinationSwitchAssessment(state, source_epoch_id, provider, network, blockers, reason)`.
  - `destination_switch_blockers(db, user_id, source_epoch_id) -> tuple[DestinationSwitchBlocker, ...]`.

- [ ] **Step 1: Write failing unit tests for blocker semantics**

Cover independently:

```python
assert codes(await destination_switch_blockers(...copy_state='ACTIVE'...)) == {'pause_required'}
assert 'positions_not_flat' in codes(...managed_size_nonzero...)
assert 'pending_jobs' in codes(...state='QUEUED'...)
assert 'pending_jobs' in codes(...state='PROCESSING'...)
assert 'pending_jobs' in codes(...state='RETRYING'...)
assert 'unresolved_executions' in codes(...state='SUBMITTING'...)
assert 'unresolved_executions' in codes(...state='UNKNOWN'...)
```

Use source `execution_epoch_id` filtering for jobs/executions so historical rows from older epochs do not block the current source.

- [ ] **Step 2: Write failing lifecycle-state tests**

Required assertions:

```python
# No TradingAccount, no SigningCredential, no historical epoch account_address.
assert assessment.state == 'NEVER_ACTIVATED'

# Any historical epoch account_address makes the missing-current-account case ambiguous.
assert assessment.state == 'UNREADABLE'

# Provider read exception never becomes NEVER_ACTIVATED.
assert assessment.state == 'UNREADABLE'
```

- [ ] **Step 3: Run RED unit tests**

Run:

```bash
cd backend
pytest -q tests/unit/test_destination_switch.py
```

Expected: FAIL because the service/types do not exist.

- [ ] **Step 4: Commit RED**

```bash
git add backend/tests/unit/test_destination_switch.py
git commit -m "test: add RED safe-switch classification coverage"
```

---

### Task 2: GREEN — implement typed local assessment without provider mutation

**Files:**
- Create: `backend/app/services/destination_switch.py`
- Test: `backend/tests/unit/test_destination_switch.py`

**Interfaces:**
- Consumes SQLAlchemy models: `User`, `TradingAccount`, `SigningCredential`, `ExecutionEpoch`, `PositionLedger`, `CopyJob`, `Execution`.
- Produces a read-only assessment API used later by `set_user_destination()`.

- [ ] **Step 1: Implement the lifecycle types and local blocker query**

Use an enum/dataclass shape equivalent to:

```python
class DestinationLifecycleState(str, Enum):
    VERIFIED_FLAT = 'VERIFIED_FLAT'
    NEVER_ACTIVATED = 'NEVER_ACTIVATED'
    UNREADABLE = 'UNREADABLE'

@dataclass(frozen=True, slots=True)
class DestinationSwitchBlocker:
    code: str
    message: str

@dataclass(frozen=True, slots=True)
class DestinationSwitchAssessment:
    state: DestinationLifecycleState
    source_epoch_id: uuid.UUID | None
    provider: ExecutionProvider
    network: Network
    blockers: tuple[DestinationSwitchBlocker, ...]
    reason: str | None = None
```

`NEVER_ACTIVATED` requires all approved DB facts, including no historical non-null epoch account address.

- [ ] **Step 2: Run unit tests**

```bash
pytest -q tests/unit/test_destination_switch.py
```

Expected: lifecycle/local tests PASS; provider-live tests are not added yet.

- [ ] **Step 3: Commit GREEN**

```bash
git add backend/app/services/destination_switch.py backend/tests/unit/test_destination_switch.py
git commit -m "feat: add destination switch lifecycle assessment"
```

---

### Task 3: RED/GREEN — add Hyperliquid live order evidence

**Files:**
- Modify: `backend/app/adapters/hyperliquid.py`
- Modify: `backend/tests/unit/test_hyperliquid_helpers.py`

**Interfaces:**
- Produces:

```python
async def frontend_open_orders(
    self,
    address: str,
    *,
    priority: Priority = Priority.RECONCILE,
) -> list[dict[str, Any]]:
    ...
```

- [ ] **Step 1: Write RED tests**

Test that the wrapper:

- calls SDK `Info.frontend_open_orders(address)` through `_read()`;
- returns an empty list unchanged;
- preserves trigger rows such as `{'isTrigger': True, ...}` rather than filtering them;
- propagates read/timeout/malformed failures to the caller instead of converting them to `[]`.

- [ ] **Step 2: Verify RED**

```bash
pytest -q tests/unit/test_hyperliquid_helpers.py -k 'frontend_open_orders'
```

Expected: FAIL because method is absent.

- [ ] **Step 3: Add minimal wrapper**

Implement using the same limiter/retry path as `user_state()`:

```python
return await self._read(
    self.info.frontend_open_orders,
    address,
    weight=WEIGHT_CHEAP_INFO,
    priority=priority,
    timeout=10,
)
```

Validate the returned top-level value is a list; malformed shapes raise rather than becoming empty.

- [ ] **Step 4: Verify GREEN and commit**

```bash
pytest -q tests/unit/test_hyperliquid_helpers.py -k 'frontend_open_orders'
git add backend/app/adapters/hyperliquid.py backend/tests/unit/test_hyperliquid_helpers.py
git commit -m "feat: add Hyperliquid open-order read for safe switching"
```

---

### Task 4: RED/GREEN — classify active Hyperliquid as VERIFIED_FLAT only with complete live evidence

**Files:**
- Modify: `backend/app/services/destination_switch.py`
- Modify: `backend/tests/unit/test_destination_switch.py`

**Interfaces:**
- `assess_destination_switch(...)` internally selects the verifier from persisted source provider/network. Production callers do not pass `verified_flat=True` or a prebuilt authorization result.

- [ ] **Step 1: Add RED cases**

Required cases:

```python
# all szi == 0 AND frontend_open_orders == []
assert assessment.state == 'VERIFIED_FLAT'

# any nonzero szi
assert 'positions_not_flat' in blocker_codes(assessment)

# any order row, including isTrigger=True
assert 'open_orders_present' in blocker_codes(assessment)

# user_state raises / malformed
assert assessment.state == 'UNREADABLE'

# frontend_open_orders raises / malformed
assert assessment.state == 'UNREADABLE'
```

Also assert an activated `risex` source without complete read implementation returns `UNREADABLE`.

- [ ] **Step 2: Verify RED**

```bash
pytest -q tests/unit/test_destination_switch.py
```

- [ ] **Step 3: Implement the provider verifier**

For Hyperliquid:

1. instantiate adapter from persisted source network;
2. read account state and open orders;
3. parse every `assetPositions[].position.szi` as `Decimal`;
4. require all sizes zero;
5. require `frontend_open_orders == []`;
6. map any exception/unsupported/malformed response to `UNREADABLE`.

For activated RISEx: return `UNREADABLE` with a non-secret reason until a complete read capability is separately implemented.

- [ ] **Step 4: Verify GREEN and commit**

```bash
pytest -q tests/unit/test_destination_switch.py
git add backend/app/services/destination_switch.py backend/tests/unit/test_destination_switch.py
git commit -m "feat: require live provider flatness for destination switch"
```

---

### Task 5: RED — make `set_user_destination()` itself reject unsafe provider/network switches

**Files:**
- Create: `backend/tests/integration/test_destination_switch_hardening.py`
- Modify later: `backend/app/services/execution_destination.py`

**Interfaces:**
- `set_user_destination()` keeps its existing caller-facing provider/network/account/credential arguments.
- Safe-switch verification is invoked internally only when persisted provider or network changes.

- [ ] **Step 1: Add integration RED cases**

Using PostgreSQL, cover:

- direct network change with `copy_state=ACTIVE` -> rejected;
- direct network change with pending `QUEUED`, `PROCESSING`, `RETRYING` -> rejected;
- direct network change with `SUBMITTING`/`UNKNOWN` execution -> rejected;
- local managed non-zero ledger -> rejected;
- source verified flat -> new epoch created;
- source `NEVER_ACTIVATED` -> new epoch created;
- `UNREADABLE` -> no epoch mutation;
- source identity changes between verification and mutation -> rejected;
- calling `close_user_destination_epoch()` first then changing provider/network -> still requires switch authorization;
- old epoch fields remain unchanged except `ended_at`;
- failed authorization leaves active epoch/user destination unchanged.

- [ ] **Step 2: Verify RED**

```bash
RUN_INTEGRATION=1 pytest -q tests/integration/test_destination_switch_hardening.py
```

Expected: unsafe direct transitions currently succeed or lack the new error contract.

- [ ] **Step 3: Commit RED**

```bash
git add backend/tests/integration/test_destination_switch_hardening.py
git commit -m "test: add RED central destination switch guard coverage"
```

---

### Task 6: GREEN — centralize authorization inside `set_user_destination()`

**Files:**
- Modify: `backend/app/services/execution_destination.py`
- Modify: `backend/app/services/destination_switch.py`
- Test: `backend/tests/integration/test_destination_switch_hardening.py`

- [ ] **Step 1: Extend the locked source query**

Under `FOR UPDATE OF u`, read both persisted user identity and active/latest source epoch identity so a detached/closed epoch cannot masquerade as first bootstrap.

Switch detection is based on persisted `users.execution_provider` / `users.execution_network`, not merely whether an active epoch exists.

- [ ] **Step 2: Preserve non-switch cases**

Do not invoke switch authorization for:

- genuine first materialization;
- exact open-epoch identity no-op;
- same-provider/same-network account or credential rebind.

Those paths keep the existing epoch-rotation semantics required by trading-account lifecycle tests.

- [ ] **Step 3: Authorize provider/network changes centrally**

Pseudocode:

```python
is_switch = persisted_provider != target_provider or persisted_network != target_network
if is_switch:
    assessment = await assess_destination_switch(db, user_id, source_identity=source)
    if assessment.state not in {VERIFIED_FLAT, NEVER_ACTIVATED} or assessment.blockers:
        raise DestinationSwitchBlocked(...)
    await recheck_local_blockers(...)
    await assert_source_identity_unchanged(...)
# only now close/create epoch
```

Use a typed application exception carrying machine-readable blocker codes; do not log provider payloads or secrets.

- [ ] **Step 4: Verify integration GREEN**

```bash
RUN_INTEGRATION=1 pytest -q tests/integration/test_destination_switch_hardening.py
```

- [ ] **Step 5: Commit GREEN**

```bash
git add backend/app/services/execution_destination.py backend/app/services/destination_switch.py backend/tests/integration/test_destination_switch_hardening.py
git commit -m "feat: enforce safe switching at destination boundary"
```

---

### Task 7: RED/GREEN — reorder the existing `/trading-network` path

**Files:**
- Modify: `backend/app/api/user.py`
- Modify: `backend/tests/unit/test_user_network.py`
- Modify: `backend/tests/integration/test_destination_switch_hardening.py`

- [ ] **Step 1: Add RED regression proving cleanup does not happen before authorization**

Build a blocked switch with a real `TradingAccount`, ledger and `RiskState`, then assert after rejection:

```python
assert trading_account_still_exists
assert ledger_rows_still_exist
assert risk_state_still_exists
assert active_epoch_is_original
```

Add a successful `VERIFIED_FLAT` case asserting cleanup occurs only after the central transition decision and commits atomically.

- [ ] **Step 2: Verify RED**

```bash
RUN_INTEGRATION=1 pytest -q tests/integration/test_destination_switch_hardening.py -k 'cleanup or network_endpoint'
```

- [ ] **Step 3: Reorder endpoint code**

Required order:

```python
current = await user_network_state(...)
# optional local readiness for early UX response
next_state = await set_user_network(db, user.id, network)  # central authoritative check
# only after this succeeds:
# delete old TradingAccount/credential cascade
# clear ledger/risk state as current behavior requires
audit(...)
await db.commit()
```

Do not duplicate provider-side authorization in the endpoint.

- [ ] **Step 4: Make API blocker presentation reuse the shared local helper**

Keep existing response field names `network_switch_ready` / `network_switch_blockers` for frontend compatibility. They remain local readiness indicators, not proof of provider flatness.

- [ ] **Step 5: Verify GREEN and commit**

```bash
pytest -q tests/unit/test_user_network.py
RUN_INTEGRATION=1 pytest -q tests/integration/test_destination_switch_hardening.py
git add backend/app/api/user.py backend/tests/unit/test_user_network.py backend/tests/integration/test_destination_switch_hardening.py
git commit -m "fix: authorize network switch before local cleanup"
```

---

### Task 8: Non-regression — preserve Hyperliquid account-link/rebind and epoch fencing

**Files:**
- Modify: `backend/tests/integration/test_trading_account_epoch_lifecycle.py`
- Modify: `backend/tests/integration/test_destination_epoch_fence.py`
- Production code only if a regression is proven and the fix remains inside PR A scope.

- [ ] **Step 1: Add/adjust account-link regression assertions**

Prove:

- initial `POST /trading-account` still creates an epoch containing account/credential identity;
- relinking same provider/network rotates epoch as before;
- relink does not invoke provider/network switch authorization;
- queued job from the old credential epoch becomes stale after a successful rebind exactly as before.

- [ ] **Step 2: Adapt stale-epoch test to the stronger safe-switch semantics**

Do not leave a `QUEUED` job as the reason a network switch is expected to succeed. Bind the historical job, then move it to a terminal state before switching. After switch, assert it still fails active-destination matching because its epoch is stale.

- [ ] **Step 3: Run focused integration suite**

```bash
RUN_INTEGRATION=1 pytest -q \
  tests/integration/test_trading_account_epoch_lifecycle.py \
  tests/integration/test_destination_epoch_fence.py \
  tests/integration/test_destination_switch_hardening.py
```

- [ ] **Step 4: Commit test adaptations**

```bash
git add backend/tests/integration/test_trading_account_epoch_lifecycle.py backend/tests/integration/test_destination_epoch_fence.py
git commit -m "test: preserve Hyperliquid epoch lifecycle under safe switching"
```

---

### Task 9: Full verification and PR A review gate

**Files:** No new functional scope.

- [ ] **Step 1: Run backend quality gates locally/CI-equivalent**

```bash
cd backend
ruff check .
pyright
python -m compileall app
RUN_INTEGRATION=1 pytest -q
pip-audit -r requirements.txt
```

Run Alembic through current head using the same PostgreSQL service/commands as repository CI.

- [ ] **Step 2: Confirm prohibited scope is absent**

Diff must show:

- no `/trading-provider`;
- no frontend files;
- no Railway/workflow/ruleset changes;
- no RISEx order/writer changes.

- [ ] **Step 3: Open PR A and wait for all 7 required checks**

Report backend, frontend, landing, repository-tree, secrets, CodeQL Python, CodeQL JS/TS. Although frontend is untouched, all repository required checks must be green.

- [ ] **Step 4: Stop before merge**

Owner approval is required before merge/deployment. Production/mainnet remains untouched.
