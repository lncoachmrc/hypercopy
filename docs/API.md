# API

Base path: `/api/v1`. Production OpenAPI UI is disabled; development exposes `/docs` and `/openapi.json`.

Authentication uses a wallet-signed challenge, a short-lived HttpOnly access-session cookie, and an opaque rotating HttpOnly refresh cookie. The access JWT remains valid for 60 minutes; the refresh family has an absolute lifetime of 24 hours from the original wallet signature and never extends beyond that boundary. The browser renews the access session silently through `/auth/refresh`; a new wallet signature is required after the absolute refresh window expires, after logout/revocation, or when the refresh family is no longer valid.

Refresh rotation is delivery-recoverable. The current refresh credential creates one deterministic successor; for at most 60 seconds the predecessor remains a bounded idempotency handle that can reproduce only that same already-created successor if an HTTP response is lost or another browser tab races the rotation. The grace interval is never extended by retries and cannot extend the 24-hour family lifetime. Redis keys contain HMAC digests of refresh credentials rather than reusable plaintext tokens.

Every state-changing authenticated HTTP request requires both `X-Requested-With: HyperCopy` and the `X-CSRF-Token` returned by `/auth/verify`, `/auth/refresh`, or `/auth/session`. `/auth/refresh` can run after the access JWT has expired, so it requires the rotating HttpOnly refresh cookie plus `X-Requested-With: HyperCopy`; it does not depend on the expired access-session CSRF claim. The frontend broadcasts fresh CSRF values between same-origin tabs when supported and can recover an explicit CSRF mismatch by re-reading `/auth/session` before retrying the mutation once.

## Authentication

| Method | Path | Purpose |
|---|---|---|
| POST | `/auth/challenge` | create one-time signing challenge |
| POST | `/auth/verify` | verify wallet signature, create 60-minute access session + 24-hour refresh family |
| POST | `/auth/refresh` | rotate/recover refresh credential and issue a new access session without a wallet signature |
| GET | `/auth/session` | restore session + authoritative CSRF token; frontend may silently refresh after a 401 |
| POST | `/auth/logout` | revoke the complete session family, invalidate its access/refresh credentials, clear cookies |

## User / trading

| Method | Path | Purpose |
|---|---|---|
| GET | `/me` | profile/account/credential/risk state |
| GET | `/dashboard` | equity, PnL, drawdown, Sharpe summary |
| GET | `/positions` | current/target/delta position ledger |
| GET | `/executions` | paginated copied execution history |
| POST | `/trading-account` | verify named HL agent and store encrypted credential |
| DELETE | `/trading-account` | remove credential/account and pause copying |
| GET/PUT | `/risk-profile` | read/update risk policy; plan limits are clamped server-side |
| POST | `/copy/pause` | block new/increased exposure while reductions remain eligible |
| POST | `/copy/resume` | reconcile first, then re-enable new exposure |
| POST | `/copy/close-positions` | queue reduce-to-zero jobs for managed positions |
| WS | `/ws/events` | authenticated realtime job/system events |

## Billing

| Method | Path | Purpose |
|---|---|---|
| GET | `/subscription` | server-side entitlement state |
| POST | `/subscription/checkout` | Stripe Checkout URL |
| POST | `/subscription/portal` | Stripe Billing Portal URL |
| POST | `/webhooks/stripe` | signed, idempotent Stripe webhook |

A Checkout success redirect never grants entitlement. Only a verified Stripe subscription webhook mutates the authoritative subscription status.

## Admin

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/system` | queue, worker, rate, lease and safety state |
| GET | `/admin/users` | paginated follower list |
| GET | `/admin/users/{id}` | follower detail |
| POST | `/admin/users/{id}/pause` | auditably pause new exposure |
| POST | `/admin/users/{id}/resume` | admin resume |
| POST | `/admin/users/{id}/reconcile` | queue full exchange reconciliation |
| POST | `/admin/system/pause` | global pause for new exposure |
| POST | `/admin/system/emergency-stop` | emergency block on new exposure; confirmation required |
| POST | `/admin/system/live-trading` | third mainnet activation gate; SUPERADMIN only |
| POST | `/admin/system/live-trading/disable` | disable persistent mainnet execution gate |
| POST | `/admin/system/resume` | clear global/emergency pause |
| GET | `/admin/audit` | append-only audit stream |

## Health / metrics

`GET /health/live` proves process liveness. `GET /health/ready` verifies PostgreSQL, Redis and expected Alembic revision for the API. `/metrics` is intended for private/admin collection; in production it returns 404 unless `X-Metrics-Token` matches `METRICS_TOKEN`.
