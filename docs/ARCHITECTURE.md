# Architecture

## Invariants

1. GitHub `main` is the deployable source of truth; Railway is the runtime.
2. PostgreSQL is the persistent source of truth. Redis never owns the only copy of critical state.
3. The master watcher is a semantic singleton protected by PostgreSQL lease + fencing, not merely by replica count.
4. Execution workers are horizontally scalable; job ownership is enforced in PostgreSQL and transport uses Redis Streams.
5. Durable order intent precedes the external Hyperliquid effect.
6. Copying converges on target positions; it does not assume every master fill was replicated perfectly.
7. Exposure-reducing operations are allowed through business/risk halts where a valid signing credential still exists.
8. Mainnet trading is fail-closed behind three independent gates.

## Hot path

```text
Hyperliquid master fill
  → watcher verifies current fencing token
  → INSERT master_event (unique exchange_event_id)
  → INSERT one copy_job per active follower
  → COMMIT PostgreSQL
  → best-effort XADD job IDs to Redis Stream
  → N workers claim jobs with FOR UPDATE SKIP LOCKED
  → calculate target and delta from PG ledger
  → Risk Engine ALLOW / TRIM / DENY
  → INSERT Execution(SUBMITTING, deterministic cloid)
  → COMMIT
  → submit IOC + expiresAfter to Hyperliquid
  → persist terminal/UNKNOWN outcome
  → update low-latency ledger estimate
```

## Cold/reconciliation path

A coordinator elected with PostgreSQL advisory lock periodically reads the real master and follower states under the shared weighted Hyperliquid rate budget. The exchange corrects `position_ledger`, including per-asset mark prices and free-margin snapshots; target deltas produce new `RECONCILE` jobs. Confirmed executions whose granular fill rows are missing are eventually materialized from `userFillsByTime` by OID, using real exchange fill IDs rather than synthetic identifiers. Assets with unresolved `SUBMITTING/UNKNOWN` executions are not re-targeted until the external effect is resolved, prioritizing duplicate prevention over liveness.

A managed follower position converges to zero whenever the master exits that asset. Truly unmanaged positions follow the configured `STRICT`, `COEXIST`, or `MANUAL_WINS` policy; `COEXIST` is the safe default.

## Railway topology

| Service | Public Internet | Railway private network | Persistent | Scale |
|---|---:|---:|---:|---:|
| frontend | yes | no | no | 1–2 |
| api | yes | yes | no | 1–3 |
| master-watcher | no | yes | no | exactly 1 configured; lease is authoritative |
| execution-worker | no | yes | no | 1–N |
| PostgreSQL | no application domain | yes | yes | Railway managed service |
| Redis | no application domain | yes | disposable/rebuildable | Railway managed service |

## Railway deploy overlap

During `old + new` overlap, two APIs are harmless because they are stateless. Two watchers may run, but only the current lease/fencing token may persist master events. Multiple workers use distinct `RAILWAY_REPLICA_ID` consumer identities and PostgreSQL row ownership. A crash after durable order intent but before response leaves a known `cloid`, so the successor resolves the exchange state instead of blindly duplicating the order.

## Rate limiting

All REST consumers share a Redis-backed weighted sliding-window budget. Priority is `ORDER > MASTER_STATE > METADATA > RECONCILE`. The position ledger removes the need for a follower `clearinghouseState` call on every master fill; reconciliation spends those reads in controlled batches. `userFillsByTime` reserves its worst-case response weight before a history call, and the watcher invokes history replay on startup/reconnect rather than on a fixed hot-path poll.

## State recovery

The platform can be reconstructed from PostgreSQL and Hyperliquid. Redis Streams can be repopulated with `python scripts/rebuild_redis_queue.py`. The watcher checkpoint lives in PostgreSQL and is monotonic even when websocket and replay paths overlap.
