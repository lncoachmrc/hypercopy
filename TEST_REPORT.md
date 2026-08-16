# HyperCopy — Build/Test Report

Generated for the repository package on 2026-08-16.

## Verified locally in the construction environment

- Python `compileall`: PASS (`backend/app`, Alembic, tests, scripts).
- Pure unit tests available without external SDK installs: **37/37 PASS**.
  - envelope crypto
  - sizing / position targeting
  - risk asymmetry and caps
  - leverage/free-margin headroom
  - reconciliation classifier
  - execution state machine
- Railway TOML parse: PASS (API, watcher, worker, frontend).
- YAML parse: PASS (Docker Compose, CI, CodeQL, Dependabot).
- TypeScript/TSX syntactic parse: PASS (9 source files).
- `package.json` ↔ root `package-lock.json` dependency consistency: PASS.
- Repository preflight/obvious secret-assignment scan: PASS.

## Deliberately not claimed as executed here

The sandbox does not provide a Docker daemon and cannot reliably install packages from npm/PyPI. Therefore these remain CI/staging gates rather than fabricated successes:

- `npm ci && npm run typecheck && npm run build`
- full Python suite with the pinned Hyperliquid/Redis/Stripe dependencies installed
- Alembic migration against a live PostgreSQL 16 instance
- Redis Streams/consumer integration test
- Docker/Compose image build
- Hyperliquid testnet execution/reconciliation E2E
- Stripe test-mode webhook E2E
- Railway deploy/restart/overlap/rollback tests
- Railway PostgreSQL PITR restore drill
- KMS IAM/rotation drill

GitHub Actions in `.github/workflows/ci.yml` is the first authoritative build gate after push. `DEFINITION_OF_DONE.md` intentionally keeps live-infrastructure checks open until evidence exists.
