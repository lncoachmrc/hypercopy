# RISEx Read-Only Provider Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add provider-neutral execution identity and a RISEx read/simulation foundation while preserving current Hyperliquid execution and keeping every RISEx write path fail-closed.

**Architecture:** The Hyperliquid master remains the single strategy source. Follower execution gains an explicit provider + network + epoch identity. Shared targeting/risk logic consumes provider-neutral market/account types, while Hyperliquid and RISEx adapters own exchange-specific metadata, rounding, reads and identifiers. RISEx signed actions are deliberately unavailable in this milestone.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, PostgreSQL, Redis, pytest/Hypothesis, React/TypeScript, Railway, Hyperliquid Python SDK, direct HTTP client for RISEx official API.

**Spec:** `docs/superpowers/specs/2026-09-09-risex-follower-integration-design.md`

## Global Constraints

- Master source remains Hyperliquid only.
- `execution_provider` and `execution_network` are independent dimensions.
- `key_provider` remains encryption/key-wrapping provider.
- Existing followers default/backfill to `hyperliquid` without changing current network or copy state.
- No RISEx signed action is reachable in this milestone.
- Builder fee remains disabled.
- No automatic exchange fallback.
- Exchange reads may fail closed but must never become an inferred zero position.
- Current Hyperliquid mainnet writer fence and strategy-intent fence remain intact.
- Do not modify `ENABLE_LIVE_TRADING`, DB `live_trading`, user copy state, or Railway production configuration.
- No deployment to the current Railway `production` environment; an isolated staging/test environment is required before runtime deployment validation.
- Use TDD: failing behavior test first, then minimal implementation, then full relevant regression suite.

---

### Task 1: Make market minimums provider-specific without regressing Hyperliquid

**Files:**
- Modify: `backend/app/engine/sizing.py`
- Modify: `backend/app/adapters/hyperliquid.py`
- Modify: `backend/tests/unit/test_sizing.py`
- Review regressions: all tests constructing `AssetSpec`

**Interfaces:**
- `AssetSpec(name: str, sz_decimals: int, max_leverage: int, only_isolated: bool=False, min_notional: Decimal=Decimal("10"))`
- `plan(..., min_notional=Decimal("0"), ...)` uses `max(user_min_notional, spec.min_notional)`.
- Hyperliquid `asset_spec()` explicitly sets `min_notional=Decimal("10")`.

- [ ] **Step 1: Write the failing test**

Add a unit test proving a provider market with a $1 exchange floor can produce an actionable $5 order when the user floor is also $1, while the existing default `AssetSpec` behavior remains $10 for backward compatibility.

```python
def test_provider_market_can_define_minimum_below_hyperliquid_default():
    master = MasterExposure('BTC', D('0.05'), D('100'), D('100'))
    follower = FollowerState('u', D('100'))
    spec = AssetSpec('BTC', 2, 20, min_notional=D('1'))
    result = plan(master, follower, spec, min_notional=D('1'))
    assert result.actionable
    assert result.notional == D('5')
```

- [ ] **Step 2: Run the test and verify RED**

Run: `pytest backend/tests/unit/test_sizing.py -q`
Expected: assertion failure because the current implementation forces `EXCHANGE_MIN_NOTIONAL == 10`.

- [ ] **Step 3: Implement the minimal provider-specific floor**

Add `min_notional` to `AssetSpec`, default it to `Decimal("10")` for compatibility, and change both normal and reversal planning floors to `max(min_notional, spec.min_notional)`. Keep Hyperliquid metadata explicit at `$10`.

- [ ] **Step 4: Run sizing/risk regressions**

Run:
`pytest backend/tests/unit/test_sizing.py backend/tests/unit/test_risk.py backend/tests/unit/test_property_financial_invariants.py backend/tests/unit/test_max_positions_runtime_cap.py backend/tests/unit/test_reversal_secondary_risk.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

Commit: `refactor: make market minimum provider specific`

---

### Task 2: Extract provider-neutral execution types

**Files:**
- Create: `backend/app/adapters/base.py`
- Modify: `backend/app/adapters/hyperliquid.py`
- Create: `backend/tests/unit/test_execution_provider_types.py`

**Interfaces:**

```python
ExecutionProvider = Literal['hyperliquid', 'risex']

@dataclass(frozen=True, slots=True)
class DestinationIdentity:
    provider: ExecutionProvider
    network: Network
    epoch_id: uuid.UUID
    account_address: str

@dataclass(frozen=True, slots=True)
class AccountSnapshot: ...
@dataclass(frozen=True, slots=True)
class OrderOutcome: ...
@dataclass(frozen=True, slots=True)
class PositionConfig: ...
```

`HyperliquidAdapter.provider == 'hyperliquid'` and continues returning the extracted shared types.

- [ ] Write tests asserting provider/network are separate and `DestinationIdentity` equality changes if either provider or epoch differs.
- [ ] Run tests and observe RED because `app.adapters.base` does not yet expose these types.
- [ ] Move only common dataclasses/types out of `hyperliquid.py`; do not alter signing or exchange behavior.
- [ ] Run Hyperliquid adapter/unit tests including mainnet single-writer and strategy-intent fences.
- [ ] Commit: `refactor: extract execution provider types`.

---

### Task 3: Add execution epochs and provider persistence additively

**Files:**
- Create: `backend/alembic/versions/0011_execution_destination_epochs.py`
- Modify: `backend/app/models/entities.py`
- Create: `backend/tests/integration/test_execution_destination_migration.py`

**Interfaces:**

Create `execution_epochs` with at least:
- `id UUID PK`
- `user_id UUID FK users.id`
- `provider VARCHAR(24)` constrained to `hyperliquid|risex`
- `network VARCHAR(16)` constrained to `mainnet|testnet`
- `account_address VARCHAR(128) NULL`
- `credential_version INTEGER NULL`
- `started_at TIMESTAMPTZ NOT NULL`
- `ended_at TIMESTAMPTZ NULL`
- unique partial invariant: one active epoch per user (`ended_at IS NULL`)

Add `users.execution_provider` default/backfill `hyperliquid` and `users.active_execution_epoch_id` nullable FK during migration. Preserve existing `execution_network` and `network_started_at` during compatibility rollout.

- [ ] Write migration integration tests first: existing user rows become Hyperliquid users, their network remains unchanged, and one active epoch is created for an existing operational configuration without touching copy state.
- [ ] Verify RED before migration exists.
- [ ] Implement `0011` with `down_revision = '0010_user_plan_discounts'` and additive downgrade.
- [ ] Update ORM entities with `ExecutionEpoch` and provider fields.
- [ ] Run Alembic upgrade/downgrade/upgrade in integration DB plus migration tests.
- [ ] Commit: `feat: add execution destination epochs`.

---

### Task 4: Replace network-only operational state with destination state while preserving compatibility

**Files:**
- Create: `backend/app/services/execution_destination.py`
- Modify: `backend/app/services/networking.py`
- Modify: `backend/app/api/auth.py`
- Modify: `backend/tests/unit/test_live_mainnet_operational_baseline.py`
- Create: `backend/tests/integration/test_execution_destination_state.py`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class UserDestinationState:
    provider: ExecutionProvider
    network: Network
    epoch_id: uuid.UUID
    started_at: datetime

async def user_destination_state(db, user_id) -> UserDestinationState: ...
```

Keep `user_network_state()` as a temporary compatibility wrapper reading `user_destination_state().network`; do not create divergent state sources.

- [ ] Write failing tests for existing-user Hyperliquid default and provider/network independence.
- [ ] Implement destination lookup and compatibility wrapper.
- [ ] New-user onboarding creates an explicit Hyperliquid epoch using configured follower network.
- [ ] Run auth/network baseline tests.
- [ ] Commit: `feat: add follower destination state`.

---

### Task 5: Bind jobs and executions to immutable destination epochs

**Files:**
- Create: `backend/alembic/versions/0012_bind_execution_epoch.py`
- Modify: `backend/app/models/entities.py`
- Modify: `backend/app/services/copy.py`
- Modify: `backend/app/services/strategy_intents.py`
- Modify: `backend/app/services/execution.py`
- Modify: `backend/app/workers/execution_worker.py`
- Create: `backend/tests/integration/test_destination_epoch_fence.py`

**Interfaces:**

Add immutable fields/references needed for reconstruction:
- `copy_jobs.execution_epoch_id`
- `copy_jobs.execution_provider`
- `copy_jobs.execution_network`
- provider-local market ID in job context or typed column
- `executions.execution_epoch_id`
- `executions.execution_provider`
- `executions.execution_network`
- preserve internal execution ID separately from provider order ID

The worker must re-read the active destination immediately before an external effect and reject a stale job whose bound epoch/provider/network no longer matches.

- [ ] Write integration test: job bound to epoch A becomes locally non-executable after active epoch changes to B.
- [ ] Verify RED.
- [ ] Add additive migration and persistence.
- [ ] Extend strategy-intent evidence from `follower_network` to full destination identity without weakening the fresh-master-position check.
- [ ] Run stale-intent, single-writer, duplicate/retry and reversal tests.
- [ ] Commit: `feat: fence jobs by execution destination epoch`.

---

### Task 6: Add RISEx read-only adapter with hard write gate

**Files:**
- Create: `backend/app/adapters/risex.py`
- Create: `backend/app/adapters/risex_types.py`
- Modify: `backend/requirements.txt` only if no existing HTTP client is suitable
- Create: `backend/tests/unit/test_risex_readonly_adapter.py`
- Create: `backend/tests/unit/test_risex_write_gate.py`

**Interfaces:**

```python
class ProviderWriteDisabled(RuntimeError): ...

class RISExAdapter:
    provider: Literal['risex'] = 'risex'
    writes_enabled: Literal[False] = False

    async def system_config(self) -> dict: ...
    async def eip712_domain(self) -> dict: ...
    async def market_metadata(self) -> ...: ...
    async def account_snapshot(self, address: str, ...) -> AccountSnapshot: ...

    async def place_ioc(...):
        raise ProviderWriteDisabled(...)
```

Runtime addresses/domain/type hashes are obtained from official runtime configuration and validated; do not hardcode contract addresses from examples.

- [ ] Write write-gate test first and verify RED.
- [ ] Implement `ProviderWriteDisabled` and make every signed/mutating method fail locally before signing/network I/O.
- [ ] Add fixture-driven tests for public config, market metadata, account reads and malformed/stale responses; no live test is required in ordinary CI.
- [ ] Ensure read errors raise explicit unavailable/stale errors and never return zero positions by default.
- [ ] Run adapter tests.
- [ ] Commit: `feat: add fail closed RISEx read adapter`.

---

### Task 7: Expose provider/readiness in API and dashboard without enabling RISEx execution

**Files:**
- Modify: `backend/app/schemas/user.py`
- Modify: `backend/app/api/user.py`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/Settings.tsx`
- Modify: translations used by Settings
- Create/modify backend API tests and frontend tests for provider selection/readiness

**Interfaces:**

API returns:
- `execution_provider`
- `execution_network`
- `execution_epoch_id`
- `provider_readiness`
- provider switch blockers

RISEx selectable state may be configured/read, but activation remains blocked with an explicit reason such as `RISEx execution authorization not yet approved` until Phase 4 security acceptance passes.

- [ ] Write API/frontend tests proving Hyperliquid remains current default and RISEx readiness is non-executable.
- [ ] Verify RED.
- [ ] Implement schema/API serialization and Settings UI provider control.
- [ ] Preserve master-account exclusion independently of follower provider controls.
- [ ] Run backend API and frontend test/build suites.
- [ ] Commit: `feat: expose execution provider readiness`.

---

### Task 8: Deployment watch paths, auditability and milestone verification

**Files:**
- Modify as needed: `backend/railway.toml`, `backend/railway.worker.toml`, `backend/railway.watcher.toml`
- Modify: `.env.example`
- Modify: `RUNBOOK.md` and/or `DEPLOYMENT.md`
- Modify: repository-tree manifest if repository policy requires generated update

**Requirements:**
- API and execution-worker watch patterns include provider base types, RISEx read adapter, destination service and new migrations where relevant.
- No production variable is changed.
- Document unresolved Phase 4 signer least-privilege gate and rollback semantics.

- [ ] Add/adjust release-preflight tests if watch-pattern coverage is enforced.
- [ ] Run full backend test suite, lint/type checks configured by CI, frontend test/build, secret scan, CodeQL/CI via PR.
- [ ] Verify diff contains no secrets and no live-trading enablement.
- [ ] Verify Railway `production` remains unchanged.
- [ ] Commit: `chore: complete RISEx read foundation verification`.

---

## Explicitly Out of Scope Until a Separate Phase 4 Plan

- Registering/revoking a real RISEx signer from TRAXION.
- Persisting a RISEx private signer credential for live use.
- Any RISEx signed order, leverage, cancel, fund movement or builder approval.
- Mainnet RISEx orders.
- Merge to `main` or deployment to Railway production.
- Enabling live trading or changing existing users' copy state.

## Review Gates

After each task:
1. review the diff against the spec;
2. run the task-specific tests;
3. inspect PR CI;
4. confirm no production Railway mutation;
5. proceed only if the task remains fail-closed for RISEx writes.

At completion, use `superpowers:verification-before-completion` and `superpowers:finishing-a-development-branch` before proposing merge/deployment.
