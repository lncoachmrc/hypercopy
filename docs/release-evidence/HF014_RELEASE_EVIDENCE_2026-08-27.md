# HF-014 — Release evidence pack

Status: **PARTIAL / RELEASE GATE REMAINS BLOCKED**

Pack revision: **2**

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
- verdict: `PASS`, `FAIL`, `PARTIAL`, or `NON VERIFICABILE`;
- reviewer and review date for every completed `PASS` result.

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
- Reviewer: automated CI evidence verified by the TRAXION technical remediation session.
- Review date: 2026-08-27.
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
- Reviewer: automated CI evidence verified by the TRAXION technical remediation session.
- Review date: 2026-08-27.
- Verdict: **PASS — PostgreSQL concurrency evidence**.

### P0-REDGREEN — negative-control proof

- Commit carrying evidence: `37045d97c2102b7d177989eaeea4c93f57c32d27` and descendants.
- Environment: GitHub Actions.
- Failure injection: temporary test-only negative controls neutralized HF-001 quarantine and HF-002 serialization.
- Expected: the two dedicated P0 regressions fail for the intended reasons, then pass with production protections restored.
- Observed: negative-control CI failed exactly the intended two P0 tests; final branch CI passed with the harness removed.
- Evidence: PR #87 and `docs/release-evidence/P0_REGRESSION_2026-08-26.md`.
- Final exchange state: simulated/not applicable.
- Final local state: protected implementation restored; both P0 regressions green.
- Reviewer: automated CI evidence verified by the TRAXION technical remediation session.
- Review date: 2026-08-27.
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
- Reviewer: automated CI evidence verified by the TRAXION technical remediation session.
- Review date: 2026-08-27.
- Verdict: **PASS — high-fidelity fake exchange; real TESTNET partial fill remains unverified**.

## SPEC.md mandatory 20-case TESTNET matrix

This table mirrors the source-of-truth cases in `SPEC.md` §33. None of the rows may be omitted from a future HF-014 closure decision.

| # | Mandatory case | Required assertion | Current evidence | Verdict |
| ---: | --- | --- | --- | --- |
| 1 | Open long | correct delta | no complete real TESTNET run in this pack | **NON VERIFICABILE** |
| 2 | Increase long | correct delta | no complete real TESTNET run | **NON VERIFICABILE** |
| 3 | Reduce long | correct delta + reduce-only | deterministic tests exist; no real TESTNET release run | **PARTIAL** |
| 4 | Close long | correct delta + reduce-only | deterministic tests exist; no real TESTNET release run | **PARTIAL** |
| 5 | Open short | correct inverted sign/delta | no complete real TESTNET run | **NON VERIFICABILE** |
| 6 | Increase short | correct inverted sign/delta | no complete real TESTNET run | **NON VERIFICABILE** |
| 7 | Reduce short | correct inverted sign + reduce-only | deterministic tests exist; no real TESTNET release run | **PARTIAL** |
| 8 | Close short | correct inverted sign + reduce-only | deterministic tests exist; no real TESTNET release run | **PARTIAL** |
| 9 | Reverse long→short | two executions, close `c` then open `o` | deterministic reversal logic exists; no real TESTNET release run | **PARTIAL** |
| 10 | Reverse short→long | two executions, close `c` then open `o` | deterministic reversal logic exists; no real TESTNET release run | **PARTIAL** |
| 11 | Partial fill | residual absorbed next cycle | high-fidelity fake exchange PASS; real TESTNET not forced | **PARTIAL** |
| 12 | Rejected order | `REJECTED` reason; no retry if deterministic | current parser/unit evidence only; HF-004 remains open | **PARTIAL** |
| 13 | Rate limit | shared bucket serializes, no unsafe bypass, order priority | limiter tests/design exist; real egress/address quota not proven | **PARTIAL** |
| 14 | Duplicate event | one durable effect / deduplication | idempotency constraints exist; dedicated TESTNET release drill absent | **PARTIAL** |
| 15 | Lost event | reconciler creates recovery job | reconciliation coverage exists; real stream-loss drill absent | **PARTIAL** |
| 16 | WS reconnect | full reconciliation; no duplicate | reconnect/replay design exists; real stream drill absent | **NON VERIFICABILE** |
| 17 | Worker crash mid-flight | durable `SUBMITTING` resolved by CLOID | HF-001 integration coverage; actual process-kill drill absent | **PARTIAL** |
| 18 | Watcher crash | lease reacquired within 15 s | lease design exists; two-process/restart drill absent | **NON VERIFICABILE** |
| 19 | Insufficient collateral | `REJECTED`, notification, no deterministic retry | parser/risk behavior exists; real TESTNET release run absent | **PARTIAL** |
| 20 | Kill switch in flight | current in-flight job completes safely; following jobs denied | safety controls exist; in-flight TESTNET drill absent | **NON VERIFICABILE** |

## Additional HF-014 failure / environment matrix

The following scenarios extend the 20-case TESTNET matrix with the mandatory chaos and operational release evidence from `SPEC.md`, the audit, and the runbook.

| Scenario | Required environment | Current evidence | Verdict |
| --- | --- | --- | --- |
| Hyperliquid adapter calls on real TESTNET | staging + dedicated test agent wallet | No complete evidence pack on this baseline | **NON VERIFICABILE** |
| IOC no liquidity / market no liquidity | fake/fixture + TESTNET | current behavior not yet consolidated as HF-004 release evidence | **NON VERIFICABILE** |
| Min notional / precision edge | CI + TESTNET metadata | existing unit coverage; HF-008/HF-015 remain | **PARTIAL** |
| 429 / 5xx / timeout burst | isolated simulator/staging | no complete failure-injection run | **NON VERIFICABILE** |
| Worker crash before external submit | isolated worker + PostgreSQL | no process-kill run | **NON VERIFICABILE** |
| Worker crash after durable submit / before ack | isolated worker + fake/testnet exchange | HF-001 state-machine coverage; no process-kill run | **PARTIAL** |
| Worker crash after fill / before persist | isolated worker + fake/testnet exchange | no process-kill run | **NON VERIFICABILE** |
| Worker crash after UNKNOWN / before resolver | isolated worker + PostgreSQL | resolver regression exists; no process-kill run | **PARTIAL** |
| Redis unavailable / restart / flush and rebuild | isolated Redis + PostgreSQL | durable DB fallback/rebuild design exists; no complete flush drill recorded | **NON VERIFICABILE** |
| PostgreSQL unavailable for 30 s during job processing | isolated staging | runbook only | **NON VERIFICABILE** |
| Slow PostgreSQL | isolated staging | no load/failure run | **NON VERIFICABILE** |
| Master watcher split-brain / two processes | isolated staging + PostgreSQL | fencing design exists; no completed two-process release drill | **NON VERIFICABILE** |
| Hyperliquid WS disconnect/reconnect | staging/testnet stream | reconnect/replay design exists; no current release run | **NON VERIFICABILE** |
| Duplicate event delivery under chaos | isolated staging/testnet | durable dedup design; no recorded chaos run | **NON VERIFICABILE** |
| Injected network latency | isolated staging | no recorded failure run | **NON VERIFICABILE** |
| Forced partial fill under chaos | TESTNET or high-fidelity simulator | high-fidelity fake lifecycle PASS; real forced event absent | **PARTIAL** |
| Railway redeploy while jobs pending / under load | staging | not executed in this remediation | **NON VERIFICABILE** |
| Rolling version overlap | staging | not executed | **NON VERIFICABILE** |
| Browser E2E / session expiry and reconnect | staging browsers/devices | wallet-login regression automated; full browser E2E not completed | **NON VERIFICABILE** |
| PostgreSQL backup / PITR restore | isolated restored DB | runbook documents procedure; restore drill not executed | **NON VERIFICABILE** |
| Rollback deployment | staging | procedure documented; drill not executed here | **NON VERIFICABILE** |
| KMS rotation / rewrap | isolated KMS/staging | code/runbook only | **NON VERIFICABILE** |
| Mainnet shadow ≥ one week | production MAINNET read-only/shadow | requires separate explicit mainnet authorization and observation period | **NON VERIFICABILE** |
| Controlled canary | production MAINNET | requires separate explicit authorization after all gates | **NON VERIFICABILE** |

## Exit criteria for HF-014

HF-014 is not considered closed by merging this document. Closure requires a later evidence revision in which:

1. **all 20 mandatory `SPEC.md` TESTNET rows above have explicit executed evidence and the required PASS verdicts**;
2. ambiguous crash/recovery process-kill drill passes;
3. WS reconnect/replay passes;
4. Redis loss/rebuild passes;
5. PostgreSQL failure/recovery passes;
6. the real TESTNET adapter/trading matrix validates exchange integration;
7. nonce/topology policy is verified before signer concurrency or scale-out;
8. current staging deployment/health evidence exists for the candidate commit;
9. backup/restore and rollback evidence exists before full production;
10. mainnet shadow and controlled canary are performed only after separate explicit authorization.

The unique chaos pass criterion remains: **zero duplicate orders and convergence to the correct state within two reconciliation cycles** for the covered trading scenarios.

## Safety statement

No real-money order, MAINNET canary, production variable change, Railway replica change, secret exposure, destructive migration, or database restore was performed to produce this evidence pack.
