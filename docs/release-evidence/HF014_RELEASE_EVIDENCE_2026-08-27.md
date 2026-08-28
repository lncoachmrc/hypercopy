# HF-014 — Release evidence pack

Status: **PARTIAL / RELEASE GATE REMAINS BLOCKED**

Pack revision: **4**

Revision date: **2026-08-29**

This document is the versioned evidence index for the HF-014 release-process finding. A code path, runbook entry, green CI job, simulator result, or deployment status is not promoted to real-environment evidence unless the corresponding scenario was actually executed in the named environment.

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

`PASS` is reserved for actually executed evidence. Simulator/fake-exchange evidence is labeled as such and does not certify Hyperliquid TESTNET or MAINNET behavior. A GitHub/Railway deployment status confirms the provider-reported deployment state only; it does not substitute for direct service logs, exchange state, or an executed failure drill.

## Revision 4 scope

Revision 4 incorporates remediation evidence completed after revision 3:

- TRX-NEW-001 / PR #103 — observability fallback survives rollback/failure isolation across multiple users;
- HF-011 / PR #102 — repository-tree evidence automated on the exact PR head;
- TRX-NEW-002 / PR #105 — post-risk minimum-notional fence includes final lot-size rounding;
- PR #104 — deterministic property-based financial invariants and adversarial AI-output tests;
- HF-013 / PR #106 — structured logging severity/event codes and Uvicorn stdout normalization;
- HF-016 / PR #107 — targeted preflight clarified and authoritative full-history Gitleaks gate made explicit and fail-closed.

No new real Hyperliquid TESTNET trade, destructive staging failure injection, Redis flush, PostgreSQL outage, Railway process kill, MAINNET shadow run, or real-money canary was executed for this revision. Therefore no simulator/CI result below is promoted to a real TESTNET PASS.

## Current baseline

| Field | Value |
| --- | --- |
| Repository | `lncoachmrc/hypercopy` |
| Baseline commit | `a8fc6a494d04754ec582f9b56a0f2aaf067e0940` |
| Environment for automated evidence | GitHub Actions CI with PostgreSQL 16 + Redis 8 services |
| Staging deployment observation | GitHub combined commit status on the baseline reports `SUCCESS` for `HyperCopy - api`, `frontend`, `execution-worker`, `master-watcher`, and `ai-intelligence-worker`; direct Railway deployment/log inspection is unavailable in the current session, so this is provider-status evidence only |
| Direct public staging probe | Not available from the current tool session; no HTTP result is promoted to evidence |
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

### P1-HF007 — durable fill attribution and reversal flatten guard

- Final PR head: `0fcffb9974c161deef3cd7e9f0a6cd5e06260f9f`; merged by PR #95 into `main` as `2c7ee8eb2fed355fd20a9873de9587a89c4c4c9e`.
- Environment: GitHub Actions, PostgreSQL 16 + Redis 8, deterministic fake Hyperliquid adapters and production parser/state-machine code.
- Failure scenarios: crash/retry after a durable fill; a second execution changes `PositionLedger.last_execution_id`; stale `exchange_verified_at`; reversal primary reduce-only leg partially fills.
- Expected: no numeric-state impersonation by another execution; stale exchange verification cannot override newer ledger attribution; reversal secondary open leg is not submitted until the close leg is provably flat; reconciliation creates a fresh residual job instead of assuming flattening.
- Observed: final PR gate passed after two Codex P1 review cycles and the final review reported no major issues.
- Evidence: PR #95; CI #498; CodeQL #491; backend PostgreSQL/Redis run with `RUN_INTEGRATION=1` completed **216 passed, 10 warnings**; Ruff, compileall, Alembic and pip-audit green; Gitleaks/frontend/landing green.
- Final exchange state: simulated/fake adapter only.
- Final local state: durable execution attribution is preserved and reversal residual exposure remains reconcilable without blind second-leg submission.
- Reviewer: Codex final review + automated CI evidence verified by the TRAXION technical remediation session.
- Review date: 2026-08-27.
- Verdict: **PASS — automated PostgreSQL/fake-exchange evidence; real TESTNET partial-fill/reversal drill remains unverified**.

### P1-HF004 — executable-plan terminal rejection fence

- Final PR head: `4eb25fba54c4b003d05fd505a8e799392184c755`; merged by PR #96 into `main` as `16ac5b2579bf53cfb101a9c5ba014fbee7042ccb`.
- Environment: GitHub Actions, PostgreSQL 16 + Redis 8, production sizing/Risk Engine code with deterministic reconciliation adapters.
- Failure scenarios: `retry_policy=NONE` rejection with unchanged desired target and real position while risk caps/headroom change the actual submitted size; AI execution influence below `1.0` changes the effective source position used by the worker.
- Expected: an unchanged executable order remains fenced; a materially changed Risk Engine submitted size releases the stale terminal fence; AI scaling is identical between reconciliation preview and execution job semantics; missing executable-plan evidence remains fail-closed.
- Observed: PostgreSQL regressions cover both the risk-cap change and AI factor `0.80`; the final Codex review produced no new P1/P2 and reacted `👍` on the PR after the final-head review request.
- Evidence: PR #96; CI #502; CodeQL #495; backend PostgreSQL/Redis run with `RUN_INTEGRATION=1` completed **218 passed, 10 warnings**; Ruff, compileall, Alembic and pip-audit green; Gitleaks/frontend/landing green.
- Final exchange state: simulated/not applicable; no real TESTNET terminal rejection was forced.
- Final local state: deterministic rejection fencing now includes the durable `Execution.requested_size` and current risk-limited submitted-size preview, including AI scaling parity.
- Reviewer: Codex final review + automated CI evidence verified by the TRAXION technical remediation session.
- Review date: 2026-08-27.
- Verdict: **PASS — automated PostgreSQL evidence; real TESTNET terminal-rejection drill remains unverified**.

### TRX-NEW-001 — observability rollback failure isolation

- Final PR head: `c2f728099413d9dc8e38f129fbb0be5b9d48158c`; merged by PR #103 into `main` as `1e03882f591ad27562c241f52c8aeec061d3a42f`.
- Environment: GitHub Actions, PostgreSQL integration test, deterministic follower adapter.
- Failure injection: first follower snapshot fails and triggers `db.rollback()` while two users are processed; fallback is executed again on the next cycle.
- Expected: ORM rollback cannot abort maintenance through expired `User` objects; later users still refresh; no `CopyJob` or IOC submission is created by the observability-only path; next cycle continues.
- Observed: regression passed after converting the iteration boundary to scalar user IDs; full backend suite passed.
- Evidence: PR #103; CI #568; CodeQL #561; dedicated `test_trx_new_001_observability_rollback.py`.
- Final exchange state: simulated; order placement spy remained at zero.
- Final local state: first-user failure is isolated, second user receives snapshots in both cycles, fallback remains non-trading.
- Reviewer: Codex final review + automated CI evidence verified by the TRAXION technical remediation session.
- Review date: 2026-08-28.
- Verdict: **PASS — PostgreSQL failure-isolation evidence; not a real Hyperliquid 5xx/staging drill**.

### HF-011 — repository-tree evidence on exact head

- Final PR head: `2793b43454528e7921a9712fbcaef59b0538f243`; merged by PR #102 into `main` as `54dc2a20281ae58e450a6118bc38142fb1ef026f`.
- Environment: GitHub Actions.
- Expected: repository-tree evidence must be generated from the exact PR head, not a synthetic merge commit, and artifact identity must carry that SHA.
- Observed: final `repository-tree` job checked out/asserted the final head and uploaded `repository-tree-2793b43454528e7921a9712fbcaef59b0538f243`.
- Evidence: PR #102; CI #570; CodeQL #563; repository-tree artifact.
- Final exchange/local trading state: not applicable; CI/release tooling only.
- Reviewer: Codex final review + automated CI evidence verified by the TRAXION technical remediation session.
- Review date: 2026-08-28.
- Verdict: **PASS — release evidence automation**.

### TRX-NEW-002 — post-risk executable minimum notional

- Final PR head: `bfa03cf48071fd9119e117b13320e29a0c5759fd`; merged by PR #105 into `main` as `f5cdc133362713532f75b535a84dabc910997fe2`.
- Environment: GitHub Actions, production sizing/Risk Engine code with deterministic unit tests.
- Failure scenario: `plan()` is initially executable above the exchange minimum, then Risk Engine headroom trims it to or near `$10`; execution would later round size to the lot step.
- Expected: the risk decision must use the actually submit-able post-round size/notional; if rounding drops below `$10`, deny rather than emit an actionable sub-minimum trim; valid trims must remain within risk cap.
- Observed: Codex first identified the sub-$10 post-risk trim gap and then a P1 where theoretical `$10` became `$9.60` after BTC lot rounding; final implementation rounds the trim first, recomputes notional, and denies below minimum. Final review reacted `👍`.
- Evidence: PR #105; CI #575; CodeQL #568; `backend/tests/unit/test_risk.py` regressions for `$9`, `$10` post-round denial, first valid BTC lot and ordinary trims.
- Final exchange state: simulated/not applicable; no real TESTNET minimum-notional rejection forced.
- Final local state: `ALLOW/TRIM` cannot represent a post-risk sub-minimum opening in the covered deterministic path.
- Reviewer: Codex final review + automated CI evidence verified by the TRAXION technical remediation session.
- Review date: 2026-08-28.
- Verdict: **PASS — deterministic Risk Engine evidence; real TESTNET precision/minimum edge remains unverified**.

### P2-PROPERTY-AI — property-based financial and AI-adversarial invariants

- Final PR head: `6dbfcb2cf6eb54ede5dd03a478a22dbc7d9adcff`; merged by PR #104 into `main` as `fce71b2b3f3dec9ae4d2b3ef2641a9b7a647e304`.
- Environment: GitHub Actions, Hypothesis with deterministic settings, production pure sizing/Risk/AI normalization functions plus the full PostgreSQL/Redis backend suite.
- Generated/adversarial scenarios: quantity/price precision, unmanaged margin, reduce-only bounds, deterministic risk caps, sub-minimum post-risk headroom, NaN/infinity/extreme/malformed AI fields, invalid enums and attempted control-plane injection.
- Expected: generated cases preserve financial invariants; AI cannot increase absolute source position or inject orders/leverage/risk overrides; any executable opening after Risk Engine remains `>= $10` and `<=` deterministic headroom.
- Observed: the initial property review exposed TRX-NEW-002, which was fixed separately in PR #105; the property branch was then updated from `main`, strengthened and passed the final suite/review.
- Evidence: PR #104; CI #578; CodeQL #571; `test_property_financial_invariants.py`; `test_ai_adversarial_invariants.py`.
- Final exchange state: no exchange calls; deterministic/generated evidence only.
- Final local state: covered financial and AI advisory boundaries satisfy the generated invariants.
- Reviewer: Codex final review + automated CI evidence verified by the TRAXION technical remediation session.
- Review date: 2026-08-28.
- Verdict: **PASS — property/adversarial automated evidence; does not replace TESTNET/chaos validation**.

### HF-013 — structured logging severity and event codes

- Final PR head: `56faecdd61f55f1d160866e5ae0c0e631048aeeb`; merged by PR #106 into `main` as `bc9763c238a3e55154ed6d9cd3de6f581d723ceb`.
- Environment: GitHub Actions unit/full suite.
- Failure/observability scenario: Uvicorn WebSocket lifecycle INFO messages used dedicated default handlers/streams and could be operationally mistaken for errors; normal PostgreSQL fallback consumption was also logged as WARNING.
- Expected: root and Uvicorn loggers emit structured JSON to stdout without duplicates; semantic INFO/WARNING/ERROR remains intact; stable event codes distinguish benign lifecycle/fallback events from real failures.
- Observed: dedicated logging regressions passed; real Uvicorn errors remain ERROR and benign WS accepted/open events remain INFO; normal DB fallback consumption is INFO while fallback activation/failure remains warning/error.
- Evidence: PR #106; CI #580; CodeQL #573; `backend/tests/unit/test_logging.py`.
- Final exchange state: not applicable.
- Final local state: logging semantics normalized in code/tests.
- Staging limitation: current baseline has successful Railway provider statuses, but direct Railway log inspection is unavailable, so real staging log classification is not marked PASS.
- Reviewer: Codex final review + automated CI evidence verified by the TRAXION technical remediation session.
- Review date: 2026-08-29.
- Verdict: **PASS — automated logging behavior; PARTIAL for real staging log observation**.

### HF-016 — targeted preflight and authoritative full-history secret gate

- Final PR head: `07cf6aa078a534924aa69d846ff9d473fb70964f`; merged by PR #107 into `main` as baseline `a8fc6a494d04754ec582f9b56a0f2aaf067e0940`.
- Environment: GitHub Actions release tooling.
- Expected: targeted release preflight is clearly non-authoritative for secrets; current migrations `0001`–`0010` are registered and future numbered migrations cannot silently bypass the baseline; Gitleaks must actually scan repository history and fail closed if no history is scanned.
- Observed: Codex identified that `gitleaks-action` event triggers scan an event range even with `fetch-depth: 0`; it was replaced by the pinned official Gitleaks container using `gitleaks git` without event-range `--log-opts`. Log inspection then caught a second issue where container Git ownership produced `0 commits scanned` while the step exited successfully. The final gate injects `safe.directory`, uses `pipefail`, and asserts a positive commit count.
- Evidence: PR #107; CI #584; CodeQL #577. Final `secrets` log reports **592 commits scanned**, approximately **2.62 MB**, and **no leaks found**. `repository-tree` also executes `scripts/targeted_release_preflight.py` successfully.
- Final exchange/local trading state: not applicable; release/security tooling only.
- Reviewer: Codex final review + automated CI/log evidence verified by the TRAXION technical remediation session.
- Review date: 2026-08-29.
- Verdict: **PASS — release/security gate evidence**.

## SPEC.md mandatory 20-case TESTNET matrix

This table mirrors the source-of-truth cases in `SPEC.md` §33. None of the rows may be omitted from a future HF-014 closure decision.

| # | Mandatory case | Required assertion | Current evidence | Verdict |
| ---: | --- | --- | --- | --- |
| 1 | Open long | correct delta | property/sizing tests cover deterministic bounds; no complete real TESTNET run in this pack | **PARTIAL** |
| 2 | Increase long | correct delta | property/sizing tests cover deterministic bounds; no complete real TESTNET run | **PARTIAL** |
| 3 | Reduce long | correct delta + reduce-only | deterministic/property tests exist; no real TESTNET release run | **PARTIAL** |
| 4 | Close long | correct delta + reduce-only | deterministic/property tests exist; no real TESTNET release run | **PARTIAL** |
| 5 | Open short | correct inverted sign/delta | deterministic/property tests exist; no complete real TESTNET run | **PARTIAL** |
| 6 | Increase short | correct inverted sign/delta | deterministic/property tests exist; no complete real TESTNET run | **PARTIAL** |
| 7 | Reduce short | correct inverted sign + reduce-only | deterministic/property tests exist; no real TESTNET release run | **PARTIAL** |
| 8 | Close short | correct inverted sign + reduce-only | deterministic/property tests exist; no real TESTNET release run | **PARTIAL** |
| 9 | Reverse long→short | two executions, close `c` then open `o` | HF-007 reversal guard has PostgreSQL/fake-exchange PASS; no real TESTNET release run | **PARTIAL** |
| 10 | Reverse short→long | two executions, close `c` then open `o` | deterministic reversal logic exists; no real TESTNET release run | **PARTIAL** |
| 11 | Partial fill | residual absorbed next cycle | HF-007 PostgreSQL/fake-exchange lifecycle PASS; real TESTNET not forced | **PARTIAL** |
| 12 | Rejected order | `REJECTED` reason; no retry if deterministic | HF-004 PostgreSQL executable-plan fence PASS; real TESTNET rejection run absent | **PARTIAL** |
| 13 | Rate limit | shared bucket serializes, no unsafe bypass, order priority | limiter tests/design exist; real egress/address quota not proven | **PARTIAL** |
| 14 | Duplicate event | one durable effect / deduplication | idempotency constraints exist; dedicated TESTNET release drill absent | **PARTIAL** |
| 15 | Lost event | reconciler creates recovery job | reconciliation coverage exists; real stream-loss drill absent | **PARTIAL** |
| 16 | WS reconnect | full reconciliation; no duplicate | reconnect/replay design exists; HF-013 only normalizes logging and does not prove reconnect; real stream drill absent | **NON VERIFICABILE** |
| 17 | Worker crash mid-flight | durable `SUBMITTING` resolved by CLOID | HF-001/HF-007 integration coverage plus TRX-NEW-001 failure isolation; actual process-kill drill absent | **PARTIAL** |
| 18 | Watcher crash | lease reacquired within 15 s | lease design exists; two-process/restart drill absent | **NON VERIFICABILE** |
| 19 | Insufficient collateral | `REJECTED`, notification, no deterministic retry | parser/risk behavior exists; real TESTNET release run absent | **PARTIAL** |
| 20 | Kill switch in flight | current in-flight job completes safely; following jobs denied | safety controls exist; in-flight TESTNET drill absent | **NON VERIFICABILE** |

## Additional HF-014 failure / environment matrix

The following scenarios extend the 20-case TESTNET matrix with the mandatory chaos and operational release evidence from `SPEC.md`, the audit, and the runbook.

| Scenario | Required environment | Current evidence | Verdict |
| --- | --- | --- | --- |
| Hyperliquid adapter calls on real TESTNET | staging + dedicated test agent wallet | No complete real TESTNET evidence pack on revision 4 baseline | **NON VERIFICABILE** |
| IOC no liquidity / market no liquidity | fake/fixture + TESTNET | production parser/taxonomy and liquidity-backoff predicate have unit coverage (`test_hf004_action_error_taxonomy.py`); no full fake lifecycle or real TESTNET no-liquidity run is recorded | **PARTIAL** |
| Min notional / precision edge | CI + TESTNET metadata | TRX-NEW-002 post-risk/post-round minimum fence and property invariants PASS in CI; no complete real TESTNET precision-edge run | **PARTIAL** |
| 429 / 5xx / timeout burst | isolated simulator/staging | TRX-NEW-001 proves one-user snapshot failure/rollback does not abort later-user observability and never submits orders; no repeated 429/5xx/timeout burst or staging injection executed | **PARTIAL** |
| Worker crash before external submit | isolated worker + PostgreSQL | no process-kill run | **NON VERIFICABILE** |
| Worker crash after durable submit / before ack | isolated worker + fake/testnet exchange | HF-001/HF-007 state-machine coverage; no process-kill run | **PARTIAL** |
| Worker crash after fill / before persist | isolated worker + fake/testnet exchange | HF-007 crash/retry PostgreSQL coverage; no process-kill run | **PARTIAL** |
| Worker crash after UNKNOWN / before resolver | isolated worker + PostgreSQL | resolver regression exists; no process-kill run | **PARTIAL** |
| Redis unavailable / restart / flush and rebuild | isolated Redis + PostgreSQL | durable DB fallback/rebuild design exists; no complete flush drill recorded | **NON VERIFICABILE** |
| PostgreSQL unavailable for 30 s during job processing | isolated staging | runbook only; TRX-NEW-001 rollback isolation is narrower and does not certify DB outage recovery | **NON VERIFICABILE** |
| Slow PostgreSQL | isolated staging | no load/failure run | **NON VERIFICABILE** |
| Master watcher split-brain / two processes | isolated staging + PostgreSQL | fencing design exists; no completed two-process release drill | **NON VERIFICABILE** |
| Hyperliquid WS disconnect/reconnect | staging/testnet stream | reconnect/replay design exists; HF-013 logging evidence does not certify reconnect behavior | **NON VERIFICABILE** |
| Duplicate event delivery under chaos | isolated staging/testnet | durable dedup design; no recorded chaos run | **NON VERIFICABILE** |
| Injected network latency | isolated staging | no recorded failure run | **NON VERIFICABILE** |
| Forced partial fill under chaos | TESTNET or high-fidelity simulator | HF-007 high-fidelity fake lifecycle PASS; real forced event absent | **PARTIAL** |
| Current staging deployment/health on candidate commit | staging | baseline `a8fc6a494d04754ec582f9b56a0f2aaf067e0940` has GitHub Railway `SUCCESS` statuses for api, frontend, execution-worker, master-watcher and ai-intelligence-worker; direct Railway deployment/log inspection remains unavailable | **PARTIAL** |
| Structured Uvicorn / fallback log severity in staging | staging API/worker logs | HF-013 unit/full-suite PASS; provider status is green; direct staging logs unavailable, so real stream/severity observation is not certified | **PARTIAL** |
| Full-history secret scan / targeted release preflight | GitHub Actions | HF-016 CI #584 executed targeted preflight and Gitleaks full-history scan; final log reports 592 commits scanned and no leaks | **PASS** |
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
8. current staging deployment/health evidence exists for the candidate commit, including execution-worker and direct service/log inspection;
9. backup/restore and rollback evidence exists before full production;
10. mainnet shadow and controlled canary are performed only after separate explicit authorization.

The unique chaos pass criterion remains: **zero duplicate orders and convergence to the correct state within two reconciliation cycles** for the covered trading scenarios.

## Safety statement

No real-money order, MAINNET canary, production variable change, Railway replica change, secret exposure, destructive migration, Redis flush, induced PostgreSQL outage, process kill, or database restore was performed to produce revision 4. The release gate remains BLOCKED until the missing real-environment evidence is executed and reviewed.