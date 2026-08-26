# HF-014 — Release evidence pack

Status: **PARTIAL / RELEASE GATE REMAINS BLOCKED**

This document is the versioned evidence index for the HF-014 release-process finding. A code path, runbook entry, or green unit test is not promoted to real-environment evidence unless the corresponding scenario was actually executed in the named environment.

## Evidence rules

Every scenario must record:

- repository commit;
- environment;
- precondition;
- input/event or failure injection;
- expected result;
- observed result;
- command, test ID, workflow run, or other reproducible evidence;
- final exchange state;
- final local state;
- verdict: `PASS`, `FAIL`, or `NON VERIFICABILE`;
- reviewer and date.

`PASS` is reserved for actually executed evidence. Simulator/fake-exchange evidence is labeled as such and does not certify Hyperliquid TESTNET or MAINNET behavior.

## Current baseline

| Field | Value |
| --- | --- |
| Repository | `lncoachmrc/hypercopy` |
| Baseline commit | `17355b4e20603d8cb664f30017cfe6272b11b1fa` |
| Environment for automated evidence | GitHub Actions CI with PostgreSQL 16 + Redis 8 services |
| Real-money/mainnet testing | Not authorized / not performed |
| Release gate | BLOCKED |

## Completed reproducible evidence

### P0-UNKNOWN — ambiguous execution recovery

- Commit: `5c142da95a45f21ddf2a75f47d07ffa09683b31c` and descendants.
- Environment: GitHub Actions, real PostgreSQL service, fake Hyperliquid adapter.
- Precondition: durable ambiguous `SUBMITTING/UNKNOWN` execution.
- Expected: no blind resubmit; CLOID/fill/position recovery; aged ambiguity cannot fence forever; safe reductions remain possible.
- Observed: HF-001 PostgreSQL regression suite passed after implementation.
- Evidence: PR #86; final PR CI #431 and CodeQL #424; post-merge CI/CodeQL also green.
- Final exchange state: simulated/fake adapter only.
- Final local state: terminal recovery/quarantine and authoritative ledger synchronization covered.
- Verdict: **PASS — automated integration evidence; not a real TESTNET crash drill**.

### P0-LEDGER — concurrent PositionLedger serialization

- Commit: `fe298f400ba20c5d98d6fbc10e0d31e5f095e04a` and descendants.
- Environment: GitHub Actions, PostgreSQL 16 with two real DB sessions.
- Failure injection: reconciliation absolute write races with execution fill delta.
- Expected: serialization; no lost update; next sizing uses correct ledger.
- Observed: deterministic barrier regression passed.
- Evidence: PR #85; PostgreSQL integration suite.
- Final exchange state: simulated.
- Final local state: ledger incorporates the fill and subsequent sizing sees the correct value.
- Verdict: **PASS — PostgreSQL concurrency evidence**.

### P0-REDGREEN — negative-control proof

- Commit carrying evidence: `37045d97c2102b7d177989eaeea4c93f57c32d27` and descendants.
- Environment: GitHub Actions.
- Failure injection: temporary test-only negative controls neutralized HF-001 quarantine and HF-002 serialization.
- Expected: the two dedicated P0 regressions fail for the intended reasons, then pass with production protections restored.
- Observed: negative-control CI failed exactly the intended two P0 tests; final branch CI passed with the harness removed.
- Evidence: PR #87 and `docs/release-evidence/P0_REGRESSION_2026-08-26.md`.
- Verdict: **PASS**.

### P1-PARTIAL-FILL — high-fidelity fake exchange lifecycle

- Commit: `17355b4e20603d8cb664f30017cfe6272b11b1fa`.
- Environment: GitHub Actions, PostgreSQL 16, deterministic fake Hyperliquid adapter using the production response parser.
- Input: requested `1.000 BTC`; exchange response `filled.totalSz=0.400`; residual response `0.600`.
- Expected: ledger advances by `0.400`, original CLOID is not resubmitted, reconciliation creates exactly one `0.600` residual job, final reconciliation creates zero jobs.
- Observed: integration test passed in CI #438.
- Test ID: `backend/tests/integration/test_hf007_partial_fill_lifecycle.py::test_partial_ioc_fill_updates_actual_size_then_reconciles_exact_residual`.
- Final exchange state: fake adapter position `1.000 BTC`.
- Final local state: two executions requested `[1.000, 0.600]`, filled `[0.400, 0.600]`, ledger `1.000`.
- Verdict: **PASS — high-fidelity fake exchange; real TESTNET partial fill remains unverified**.

## HF-014 failure / environment matrix

| Scenario | Required environment | Current evidence | Verdict |
| --- | --- | --- | --- |
| Hyperliquid adapter calls on real TESTNET | staging + dedicated test agent wallet | No complete evidence pack on this baseline | **NON VERIFICABILE** |
| Open long / short | TESTNET | Not executed as part of this remediation | **NON VERIFICABILE** |
| Increase / reduce / close | TESTNET | Unit/integration behavior exists; no current real TESTNET run | **NON VERIFICABILE** |
| Reverse close→open | TESTNET | Deterministic sizing coverage exists; no current real TESTNET run | **NON VERIFICABILE** |
| Partial fill | fake exchange + TESTNET | High-fidelity fake lifecycle PASS; real TESTNET partial-fill not forced | **PARTIAL** |
| IOC no liquidity / rejected order | fake/fixture + TESTNET | Existing behavior/tests are not a complete release artifact | **NON VERIFICABILE** |
| Min notional / precision edge | CI | Existing unit coverage; dedicated release evidence to be consolidated with HF-008/HF-015 | **PARTIAL** |
| 429 / 5xx / timeout burst | isolated simulator/staging | No complete failure-injection run | **NON VERIFICABILE** |
| Worker crash before submit | isolated worker + PostgreSQL | No process-kill run | **NON VERIFICABILE** |
| Worker crash after durable submit / before ack | isolated worker + fake/testnet exchange | HF-001 state-machine coverage exists; no process-kill run | **PARTIAL** |
| Worker crash after fill / before persist | isolated worker + fake/testnet exchange | No process-kill run | **NON VERIFICABILE** |
| Worker crash after UNKNOWN / before resolver | isolated worker + PostgreSQL | Resolver regression exists; no process-kill run | **PARTIAL** |
| Redis unavailable / flush and rebuild | isolated Redis + PostgreSQL | Durable DB fallback/rebuild design exists; no complete flush drill recorded | **NON VERIFICABILE** |
| PostgreSQL unavailable during job | isolated staging | Runbook only | **NON VERIFICABILE** |
| Slow PostgreSQL | isolated staging | No load/failure run | **NON VERIFICABILE** |
| Master watcher split-brain / two processes | isolated staging + PostgreSQL | Fencing design exists; no completed two-process release drill | **NON VERIFICABILE** |
| WS disconnect / reconnect / replay | staging/testnet stream | Reconnect/replay design exists; no current release run | **NON VERIFICABILE** |
| Railway redeploy while jobs pending | staging | Not executed in this remediation | **NON VERIFICABILE** |
| Rolling version overlap | staging | Not executed | **NON VERIFICABILE** |
| Browser E2E / session reconnect | staging browsers/devices | Wallet-login regression is automated; full browser E2E not completed | **NON VERIFICABILE** |
| PostgreSQL backup / PITR restore | isolated restored DB | Runbook documents procedure; restore drill not executed | **NON VERIFICABILE** |
| Rollback deployment | staging | Procedure documented; drill not executed here | **NON VERIFICABILE** |
| KMS rotation / rewrap | isolated KMS/staging | Code/runbook only | **NON VERIFICABILE** |
| Mainnet shadow | production MAINNET read-only/shadow | Requires separate explicit mainnet authorization and observation period | **NON VERIFICABILE** |
| Controlled canary | production MAINNET | Requires separate explicit authorization after all gates | **NON VERIFICABILE** |

## Exit criteria for HF-014

HF-014 is not considered closed by merging this document. Closure requires a later evidence revision where all release-gating rows have executable evidence and the required rows are `PASS`, including at minimum:

1. ambiguous crash/recovery drill;
2. WS reconnect/replay;
3. Redis loss/rebuild;
4. PostgreSQL failure/recovery;
5. real TESTNET trading matrix sufficient to validate adapter integration;
6. nonce/topology policy verified before signer concurrency or scale-out;
7. current staging deployment/health evidence for the candidate commit;
8. backup/restore and rollback evidence before full production;
9. mainnet shadow and controlled canary only after separate explicit authorization.

## Safety statement

No real-money order, MAINNET canary, production variable change, Railway replica change, secret exposure, destructive migration, or database restore was performed to produce this evidence pack.
