# P0 Regression Evidence — 2026-08-26

## Scope

This evidence closes the consolidated audit item **P0.3 — Regression evidence** for the two implemented P0 fixes:

- **HF-001** — ambiguous `Execution=UNKNOWN` must not fence an asset indefinitely;
- **HF-002** — concurrent `PositionLedger` absolute/delta writes must serialize without lost updates.

No production, Railway, mainnet, testnet wallet, signing credential, or real Hyperliquid order was used. Exchange behavior in HF-001 tests is provided by deterministic fake adapters. HF-002 uses an ephemeral PostgreSQL 16 service in GitHub Actions.

## Baseline / fixed state (GREEN)

- Repository: `lncoachmrc/hypercopy`
- Fixed `main` commit: `5c142da95a45f21ddf2a75f47d07ffa09683b31c`
- CI run: `#432` (`33005147238`)
- Backend environment: Python 3.12, PostgreSQL 16, Redis 8, `RUN_INTEGRATION=1`
- Alembic: upgrade to head succeeded
- Pytest: **137 passed, 10 warnings in 2.96s**
- `pip-audit`: **No known vulnerabilities found**
- Frontend, landing and Gitleaks: PASS
- Post-merge CodeQL `#425` (`33005147085`): Python PASS; JavaScript/TypeScript PASS

### Positive P0 test IDs

HF-001:

- `tests/integration/test_hf001_unknown_resolution.py::test_dead_unknown_is_quarantined_after_sla_without_blind_resubmit`
- `tests/integration/test_hf001_unknown_resolution.py::test_aged_unknown_with_live_job_alerts_but_keeps_fence`
- `tests/integration/test_hf001_unknown_resolution.py::test_resolver_recovers_actual_fill_and_syncs_authoritative_position`
- `tests/integration/test_hf001_unknown_resolution.py::test_recovered_fill_keeps_unknown_fence_when_snapshot_is_unavailable`

HF-002:

- `tests/integration/test_position_ledger_concurrency.py::test_reconcile_and_fill_writes_serialize_without_lost_update`
- unit coverage in `tests/unit/test_position_ledger_serialization.py`

## Negative control (RED)

A temporary test-only harness was committed on isolated branch `evidence/p0-regression-red-green` at:

`4e6c5c9401b675f3c389121731bdbb5836bdb0eb`

The harness existed only as `backend/tests/conftest.py` and deliberately neutralized the two remediations during pytest:

1. HF-002: replaced the per-user `position_ledger_lock` with a no-op async context manager;
2. HF-001: made terminal jobs ineligible for ambiguity quarantine.

It did **not** modify production/runtime source files. The temporary file was removed immediately after collecting the failure evidence and is absent from the final PR diff.

### RED CI result

- Draft PR: `#87`
- CI run: `#433` (`33007209110`)
- Backend job: `98303946610`
- Ruff: PASS
- compileall: PASS
- Alembic/PostgreSQL: PASS
- Frontend: PASS
- Landing: PASS
- Gitleaks: PASS
- Pytest: **2 failed, 135 passed, 10 warnings in 1.74s**

The only two failures were the intended P0 regression tests:

### HF-001 failure without the fix

Test:

`tests/integration/test_hf001_unknown_resolution.py::test_dead_unknown_is_quarantined_after_sla_without_blind_resubmit`

Expected with fix:

`result['quarantined'] == 1`

Observed with quarantine remediation neutralized:

`result['quarantined'] == 0`

Interpretation: after retry exhaustion the DEAD job leaves the ambiguous execution fenced, reproducing the liveness defect the resolver/quarantine fix is intended to prevent.

### HF-002 failure without the fix

Test:

`tests/integration/test_position_ledger_concurrency.py::test_reconcile_and_fill_writes_serialize_without_lost_update`

Expected with fix:

the execution fill writer must block on the per-user PostgreSQL advisory lock while reconciliation owns it.

Observed with serialization neutralized:

`Failed: DID NOT RAISE TimeoutError`

Interpretation: the second writer enters the critical section before reconciliation releases it, reproducing the concurrency condition that can cause a lost `PositionLedger` update.

## Safety / exchange state

- Real Hyperliquid submit calls: **0**
- Mainnet activity: **0**
- Testnet wallet activity: **0**
- Private keys / seed phrases / signing credentials used: **0**
- Railway configuration changes: **0**
- Database used for regression evidence: ephemeral GitHub Actions PostgreSQL only
- HF-001 fake exchange positions used by tests: deterministic simulated values only

## Evidence conclusion

**PASS — P0 regression evidence established.**

The P0 tests are sensitive to the actual remediations: they fail when the HF-001/HF-002 protections are deliberately removed and pass on the fixed `main` implementation. This is a RED/GREEN negative-control demonstration rather than a green-only CI assertion.

## Rollback / cleanup

The negative-control harness was deleted in commit:

`62185abc10a5dabfe626b972aa6492017890e973`

No negative-control code remains in the final branch diff. The only intended merge artifact from PR #87 is this evidence document.

Reviewed: 2026-08-26
