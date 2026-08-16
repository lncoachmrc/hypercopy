# Implementation notes

`SPEC.md` is preserved verbatim and is the architecture/product source of truth.

The uploaded `Hl Copytrader(1).zip` was used as an input, not blindly shipped. Its useful primitives (position targeting, rate limiting, lease ideas and tests) were retained/refactored; the final repo adds the missing production layers: FastAPI API, complete PostgreSQL model, Alembic, durable job/execution chain, Hyperliquid reconciliation, Redis Streams, Stripe, SIWE/cookie auth, frontend, Railway Config-as-Code, CI/security workflows and operations documentation.

Additional correctness changes made during final verification:

- per-asset persisted `mark_price` so book exposure is never valued with the current job's unrelated asset price;
- exchange-derived `free_margin` persisted in equity snapshots;
- leverage headroom based on `max_leverage × account_equity − current_exposure`;
- real fill materialization by Hyperliquid OID during reconciliation, without synthetic exchange fill IDs;
- worst-case reservation for `userFillsByTime` rate weight;
- replay on watcher startup/reconnect instead of continuous high-weight history polling;
- watcher write rejects expired/stale fencing lease;
- unlink/account replacement blocked while managed positions remain open;
- admin resume requires successful exchange reconciliation, matching self-service resume.

See `TEST_REPORT.md` for what was actually executed locally versus what remains a real CI/Railway/testnet gate.
