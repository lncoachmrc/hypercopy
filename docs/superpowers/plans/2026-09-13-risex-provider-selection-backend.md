# RISEx Provider Selection Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit owner-scoped backend API that switches the authenticated user's execution provider between Hyperliquid and RISEx by creating a new execution epoch through the already-hardened destination transition boundary.

**Architecture:** This plan starts only after the Hyperliquid destination safe-switch hardening PR is merged. The new endpoint supplies target provider intent; it does not implement or duplicate switching authorization. `set_user_destination()` remains the single authoritative guard, Hyperliquid remains the default, RISEx activated sources remain fail-closed when complete read evidence is unavailable, and no execution routing/write behavior changes.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, SQLAlchemy async/PostgreSQL, pytest, Ruff, Pyright.

**Spec:** `docs/superpowers/specs/2026-09-13-risex-provider-selection-safe-switch-design.md`

## Global Constraints

- PR prerequisite: the dedicated Hyperliquid destination/network safe-switch hardening PR is merged and its CI/review are green.
- Backend only. Do not touch `frontend/` in this PR.
- Hyperliquid remains the schema/application default.
- Provider must be explicitly supplied; there is no implicit fallback or default to RISEx.
- Endpoint mutates only `current_user`; no `user_id` body/path parameter.
- CSRF protection remains mandatory.
- Provider switch preserves current `execution_network` and opens a new epoch; no old epoch mutation except `ended_at`.
- Provider selection is not executable readiness and does not auto-resume strategy or enable writes.
- No execution-worker/queue writer routing changes.
- No RISEx credential persistence changes and no provider write.
- RED before GREEN. No skip/xfail, bypass, force push, workflow/ruleset or Railway changes.

---

## File Structure

### Modify

- `backend/app/schemas/user.py`
  - add explicit `TradingProviderIn` schema using `Literal['hyperliquid', 'risex']`.
- `backend/app/api/user.py`
  - add `PUT /trading-provider`;
  - serialize `execution_provider` and generic local destination-switch readiness/blockers;
  - preserve existing network fields for compatibility;
  - perform cleanup only after central transition succeeds.
- `backend/app/services/execution_destination.py`
  - only if a provider-selection integration test exposes a missing idempotent/read helper; do not change safe-switch semantics from PR A.
- `backend/tests/unit/test_user_provider.py`
  - schema, ownership shape, idempotency/local serialization tests.
- `backend/tests/integration/test_trading_provider_selection.py`
  - PostgreSQL endpoint/service flow from empty-user destination state to RISEx epoch.
- Existing auth tests only if needed to assert first-login default remains Hyperliquid.

### Explicitly untouched

- `frontend/**`;
- `backend/app/workers/execution_worker.py`;
- `backend/app/services/queue.py` provider-write restrictions;
- signed RISEx transport/permit/readiness/pre-order gate;
- Railway variables/deployments.

---

### Task 1: RED — provider request schema and ownership contract

**Files:**
- Create: `backend/tests/unit/test_user_provider.py`
- Modify later: `backend/app/schemas/user.py`

**Interfaces:**
- Produces:

```python
class TradingProviderIn(BaseModel):
    provider: Literal['hyperliquid', 'risex']
```

- [ ] **Step 1: Write schema RED tests**

```python
assert TradingProviderIn(provider='hyperliquid').provider == 'hyperliquid'
assert TradingProviderIn(provider='risex').provider == 'risex'
with pytest.raises(ValidationError):
    TradingProviderIn()
with pytest.raises(ValidationError):
    TradingProviderIn(provider='other')
```

- [ ] **Step 2: Add static/source-level owner-scope assertion**

The endpoint contract must contain no arbitrary `user_id` argument. Use route invocation/integration coverage as the authoritative proof; a lightweight source signature assertion is acceptable as supplemental regression.

- [ ] **Step 3: Verify RED**

```bash
cd backend
pytest -q tests/unit/test_user_provider.py
```

Expected: FAIL because `TradingProviderIn` and route are absent.

- [ ] **Step 4: Commit RED**

```bash
git add backend/tests/unit/test_user_provider.py
git commit -m "test: add RED provider selection API contract"
```

---

### Task 2: GREEN — add explicit provider schema only

**Files:**
- Modify: `backend/app/schemas/user.py`
- Test: `backend/tests/unit/test_user_provider.py`

- [ ] **Step 1: Implement schema**

```python
class TradingProviderIn(BaseModel):
    provider: Literal['hyperliquid', 'risex']
```

No default value.

- [ ] **Step 2: Run focused test and commit**

```bash
pytest -q tests/unit/test_user_provider.py -k 'schema or provider'
git add backend/app/schemas/user.py backend/tests/unit/test_user_provider.py
git commit -m "feat: add explicit trading provider input schema"
```

---

### Task 3: RED — owner-scoped provider endpoint and NEVER_ACTIVATED flow

**Files:**
- Create: `backend/tests/integration/test_trading_provider_selection.py`
- Modify later: `backend/app/api/user.py`

**Interfaces:**
- Produces endpoint:

```text
PUT /api/v1/trading-provider
Body: {"provider": "hyperliquid" | "risex"}
Auth: current session owner
CSRF: required
```

- [ ] **Step 1: Add empty-database-style user fixture**

Create a user matching first SIWE materialization:

- `execution_provider='hyperliquid'`;
- `execution_network='testnet'`;
- active Hyperliquid/testnet epoch with `account_address=NULL` and `credential_version=NULL`;
- no `TradingAccount`;
- no `SigningCredential`;
- `copy_state=PAUSED`;
- no local blockers.

Do not INSERT a RISEx epoch directly; the test must exercise the application endpoint/service transition.

- [ ] **Step 2: Write RED switch assertion**

Invoke the endpoint as that user and assert:

```python
assert response_provider == 'risex'
assert response_network == 'testnet'
assert new_epoch_id != old_epoch_id
assert old_epoch.ended_at is not None
assert old_epoch.provider == 'hyperliquid'
assert new_epoch.provider == 'risex'
assert new_epoch.network == 'testnet'
assert new_epoch.account_address is None
```

- [ ] **Step 3: Add ownership RED case**

Create user A and user B. Invoke route with user A's dependency/session and prove only A changes; request schema contains no target user id.

- [ ] **Step 4: Add master-source and CSRF route tests**

Use existing dependency/test patterns to prove master-source follower control is rejected and mutation requires CSRF.

- [ ] **Step 5: Verify RED and commit**

```bash
RUN_INTEGRATION=1 pytest -q tests/integration/test_trading_provider_selection.py
git add backend/tests/integration/test_trading_provider_selection.py
git commit -m "test: add RED owner-scoped provider switch flow"
```

---

### Task 4: GREEN — implement `PUT /trading-provider` as a thin central-boundary caller

**Files:**
- Modify: `backend/app/api/user.py`
- Modify: `backend/app/schemas/user.py`
- Test: `backend/tests/integration/test_trading_provider_selection.py`

- [ ] **Step 1: Import `TradingProviderIn` and canonical destination state**

Use `current_user`, `require_csrf`, `_require_follower_user`, `user_destination_state`, and `set_user_destination()` from the already-hardened PR A baseline.

- [ ] **Step 2: Implement idempotent same-provider behavior**

Pseudocode:

```python
@router.put('/trading-provider', dependencies=[Depends(require_csrf)])
async def trading_provider(body: TradingProviderIn, user=Depends(current_user), db=Depends(get_db)):
    _require_follower_user(user)
    current = await user_destination_state(db, user.id)
    if body.provider == current.provider:
        return await _serialize_user(db, user)
```

No new epoch for same provider.

- [ ] **Step 3: Implement explicit provider transition**

Call only:

```python
next_destination = await set_user_destination(
    db,
    user.id,
    provider=body.provider,
    network=current.network,
)
```

The endpoint does not reimplement pause/flat/order/job/execution checks. A blocked central transition becomes a sanitized 409 response with machine-readable blocker code(s).

- [ ] **Step 4: Cleanup old provider-local credentials only after transition authorization**

For Hyperliquid -> RISEx:

- delete the old `TradingAccount`/credential cascade only after `set_user_destination()` succeeds;
- clear provider-local ledger/risk state using the same ordering guarantees established in PR A;
- keep the entire local transition in one transaction so a later DB error rolls back both epoch and cleanup.

For RISEx -> Hyperliquid there is currently no RISEx credential persistence model to delete. The target Hyperliquid epoch remains unconfigured until the user links a fresh API wallet through `POST /trading-account`.

- [ ] **Step 5: Audit without secrets**

Record old/new provider, network and epoch IDs. Do not log signer/private-key/provider payload data.

- [ ] **Step 6: Commit only after focused GREEN**

```bash
RUN_INTEGRATION=1 pytest -q tests/integration/test_trading_provider_selection.py
git add backend/app/api/user.py backend/app/schemas/user.py backend/tests/integration/test_trading_provider_selection.py
git commit -m "feat: add owner-scoped trading provider selection"
```

---

### Task 5: RED/GREEN — provider serialization and local readiness reporting

**Files:**
- Modify: `backend/app/api/user.py`
- Modify: `backend/tests/unit/test_user_provider.py`
- Modify: `backend/tests/integration/test_trading_provider_selection.py`

**Interfaces:**
- `/me` / dashboard user serialization adds:

```json
{
  "execution_provider": "hyperliquid",
  "execution_network": "testnet",
  "destination_switch_ready": false,
  "destination_switch_blockers": []
}
```

Existing `follower_network`, `network_switch_ready` and `network_switch_blockers` remain for frontend compatibility until the later UI PR deliberately changes its contract.

- [ ] **Step 1: Write RED serialization tests**

Assert current provider/network are read from canonical destination state, not inferred from frontend/network settings.

Assert local blockers are structured and do not claim provider-side `VERIFIED_FLAT`.

- [ ] **Step 2: Implement serialization**

Reuse the shared DB-local blocker helper introduced in PR A. Do not perform provider reads on `/me` polling.

- [ ] **Step 3: Verify and commit**

```bash
pytest -q tests/unit/test_user_provider.py
RUN_INTEGRATION=1 pytest -q tests/integration/test_trading_provider_selection.py
git add backend/app/api/user.py backend/tests/unit/test_user_provider.py backend/tests/integration/test_trading_provider_selection.py
git commit -m "feat: report execution provider and local switch readiness"
```

---

### Task 6: RED/GREEN — fail-closed activated-source behavior

**Files:**
- Modify: `backend/tests/integration/test_trading_provider_selection.py`
- Production code only if PR A's central guard is not already sufficient; do not weaken or duplicate its semantics.

- [ ] **Step 1: Add activated Hyperliquid source case**

With a real source epoch account identity and provider verifier stubbed at the adapter boundary according to existing test patterns:

- zero provider positions + zero orders -> provider switch allowed;
- non-zero position -> blocked;
- open/trigger order -> blocked;
- provider read failure -> blocked.

This proves the new endpoint cannot bypass PR A.

- [ ] **Step 2: Add activated RISEx source case**

Create a current RISEx epoch with non-null `account_address`/activation history. Without complete RISEx account/order read capability, switching back to Hyperliquid must return the central `UNREADABLE` block and leave the epoch unchanged.

- [ ] **Step 3: Add NEVER_ACTIVATED RISEx return-path case**

For a RISEx epoch with no account/credential and no historical bound epoch, provider switch back to Hyperliquid is allowed via DB-proven `NEVER_ACTIVATED`.

- [ ] **Step 4: Run focused tests and commit**

```bash
RUN_INTEGRATION=1 pytest -q tests/integration/test_trading_provider_selection.py
git add backend/tests/integration/test_trading_provider_selection.py
git commit -m "test: enforce provider switch fail-closed source semantics"
```

---

### Task 7: Non-regression — SIWE default and no execution/write reachability

**Files:**
- Modify existing auth test file if a suitable one exists; otherwise add focused assertions to `backend/tests/integration/test_trading_provider_selection.py`.
- Add source/runtime regression assertions only; do not modify worker/queue/signed RISEx production files.

- [ ] **Step 1: Prove first user remains Hyperliquid**

Exercise or inspect the real first-login application path so a new user receives:

```text
execution_provider = hyperliquid
execution_network = configured follower network
```

No default or omitted request may produce RISEx.

- [ ] **Step 2: Prove provider selection does not enable writes**

Assert provider endpoint does not:

- mutate `RISEX_SIGNED_WRITES_ENABLED`;
- call `RISExAdapter.place_ioc`;
- enqueue a RISEx execution job;
- resume `copy_state`;
- modify queue/worker provider restrictions.

- [ ] **Step 3: Prove old epoch jobs remain stale**

Create a terminal/historical job bound to the pre-switch epoch; after provider switch, `job_matches_active_destination()` must be false.

- [ ] **Step 4: Commit regression tests**

```bash
git add backend/tests
git commit -m "test: preserve provider defaults and write isolation"
```

---

### Task 8: Full verification and PR B review gate

**Files:** No new functional scope.

- [ ] **Step 1: Run backend quality gates**

```bash
cd backend
ruff check .
pyright
python -m compileall app
RUN_INTEGRATION=1 pytest -q
pip-audit -r requirements.txt
```

Run Alembic through current head using repository CI's PostgreSQL command.

- [ ] **Step 2: Diff-scope audit**

Confirm:

- no `frontend/` changes;
- no `execution_worker.py` or queue writer-routing change;
- no signed RISEx transport/readiness/pre-order gate change;
- no Railway/workflow/ruleset change;
- no provider write executed by tests.

- [ ] **Step 3: Open PR B and wait for all 7 checks**

Report backend, frontend, landing, repository-tree, secrets, CodeQL Python, CodeQL JS/TS.

- [ ] **Step 4: Stop before merge**

Owner approval is required before merge. UI remains a separately tracked PR C after backend verification.
