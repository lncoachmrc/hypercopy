# P0 MAINNET single-writer fence implementation plan

**Goal:** Guarantee that exactly the Railway environment explicitly designated by `TRAXION_MAINNET_WRITER_ENVIRONMENT_ID` can reach any signed Hyperliquid MAINNET action, independently of PostgreSQL/Redis topology.

**Safety invariant:** TESTNET is unaffected. MAINNET fails closed when either writer environment ID is missing or when `RAILWAY_ENVIRONMENT_ID != TRAXION_MAINNET_WRITER_ENVIRONMENT_ID`. A boundary rejection is definitive pre-submit evidence: no SDK signed action is invoked.

## Baseline

- Base branch: `main` at `5cf39fba837c6dc95f151d6d9f7ad9bcb7517e4c`.
- PR #126 is not reusable as the final design because its writer authority is Redis-backed and does not cover the common signed-action boundary.
- PR #127 has the correct startup/configuration concept but lacks the final signed-action boundary.
- Current common boundary: `HyperliquidAdapter._signed_call()` in `backend/app/adapters/hyperliquid.py`.

## Tasks

1. Add failing unit tests for TESTNET passthrough, MAINNET designated writer, mismatch, missing expected ID, missing actual ID, final boundary ordering, `place_ioc`/CLOSE_ALL-style action coverage, and `update_leverage` coverage.
2. Add failing Settings tests for production live startup fail-closed behavior and the valid matching-ID case. Update the existing local-RSA production-live fixture so it supplies a valid writer identity.
3. Add `TRAXION_MAINNET_WRITER_ENVIRONMENT_ID`, `RAILWAY_ENVIRONMENT_ID`, and optional diagnostic `RAILWAY_ENVIRONMENT_NAME` settings. Validate matching non-empty IDs whenever `APP_ENV=production` and `ENABLE_LIVE_TRADING=true`.
4. Add a dedicated `MainnetWriterFenceError` and a testable adapter authorization helper. Invoke it inside `_signed_call()` as the last synchronous check immediately before `func(*args)`.
5. Ensure the fence exception remains a definitive local pre-submit error and is never converted to an ambiguous exchange outcome. Strategy-intent cancellation handling must not swallow it.
6. Document the single-writer gate in `SECURITY.md`, `DEPLOYMENT.md`, and `.env.example`. The designated writer value for production-candidate is its Railway environment ID; do not configure or activate it as part of this code-only PR.
7. Run/observe full GitHub CI: backend pytest, lint, pyright, compile, Alembic, frontend/landing, repository-tree, secrets/Gitleaks, CodeQL. Fix regressions before review.
8. After CI is green, supersede PR #126 and #127. Do not merge this P0 PR or deploy MAINNET without explicit operational authorization.

## Verification checkpoint before merge/deploy

- `execution-worker` in Railway `production-candidate` remains offline.
- `ENABLE_LIVE_TRADING` and PostgreSQL `system_flags.live_trading` remain false; user copy state remains PAUSED. Where connector security prevents direct DB verification, record that limitation rather than inferring state.
- No changes to staging or Railway production.
