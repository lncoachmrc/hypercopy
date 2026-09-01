# Security Notes

## Single-Writer MAINNET Strategy Order Fence

### Threat model

TRAXION runs `execution-worker` as a single logical service that may
temporarily exist as more than one independent deployment sharing the same
PostgreSQL database and Redis instance — for example a `production-candidate`
deployment in `ams` alongside `production` in `sfo` during a region migration
or a canary rollout. Both deployments poll the same `copy_jobs` table and the
same Redis stream.

Strategy orders (job origin `EVENT` or `RECONCILE`) are produced from master
account fills/reconciliation and are queued for every active follower. If two
independent deployments both believe they are the sole authority for a given
MAINNET follower wallet, they can both claim and execute the same class of
strategy job for that wallet around the same time, resulting in duplicate
signed orders being submitted to Hyperliquid. This is unacceptable for a real
MAINNET wallet handling customer funds. TESTNET has no such requirement.

The existing job-claim lock (`SELECT ... FOR UPDATE SKIP LOCKED`), the
strategy-intent coalescing fence (`app/services/strategy_intents.py`) and the
signer action lock all protect against races and stale intents *within* one
consistent deployment topology. None of them independently enforce that only
one deployment identity is authorized to submit strategy orders for a
particular MAINNET wallet in the first place. That is the gap this fence
closes.

### Design rationale

Each deployment is given an explicit, human-assigned identity via the
`EXECUTION_WORKER_IDENTITY` environment variable. Authority to submit strategy
orders for a MAINNET follower wallet is tracked in Redis:

```
hypercopy:writers:mainnet:{follower_wallet_address} -> set of authorized identities
```

- The first strategy job processed for a wallet bootstraps the set: the
  currently configured identity is atomically registered via a Lua script that
  combines `SETNX` (bootstrap lock) with `SADD` (writer set), so concurrent
  first-jobs from independent deployments race safely and only one identity
  wins.
- Every subsequent strategy job verifies membership with `SISMEMBER` before any
  order is submitted.
- The registry is deliberately **fail-closed**: any Redis error, any missing
  `EXECUTION_WORKER_IDENTITY`, or any identity mismatch results in the job being
  marked `SKIPPED` with an audit event (`WRITER_AUTHORITY_DENIED`). No order is
  ever submitted when authority cannot be positively verified.
- There is no TTL on the registry key. Redis is expected to be durable enough
  for this operational control; if it is flushed, the next strategy job simply
  re-bootstraps a fresh writer identity (safe, since only one deployment should
  be actively running strategy jobs against a given wallet at a time).
- The check is network-scoped: TESTNET strategy jobs are an unconditional
  passthrough, since TESTNET carries no real capital risk from double
  submission.
- `CLOSE_ALL` (admin-triggered emergency flatten) is exempt from this fence. It
  is an explicit, human-initiated action outside the automated strategy-copy
  path and must not be blocked by writer-identity state.

### Deployment setup

Set `EXECUTION_WORKER_IDENTITY` on every `execution-worker` deployment to a
value that uniquely names the environment, region and replica index, e.g.:

```
EXECUTION_WORKER_IDENTITY=prod-cand-ams-1
EXECUTION_WORKER_IDENTITY=prod-sfo-1
```

In production, `ENABLE_LIVE_TRADING=true` without `EXECUTION_WORKER_IDENTITY`
configured is a hard configuration error (`Settings.production_safety`) and the
service will refuse to start. This makes the fence impossible to silently
bypass by omission.

### Admin override: reassigning a wallet to a new writer

Writer identities are never automatically revoked. To manually reassign
authority for a wallet (for example, after decommissioning the previously
authorized deployment), delete the Redis key for that wallet:

```
DEL hypercopy:writers:mainnet:{follower_wallet_address}
```

The next strategy job processed for that wallet will bootstrap a new writer
identity from whichever deployment claims it first. Operators should ensure
only the intended deployment is actively processing jobs for that wallet
before deleting the key, to avoid an unintended deployment winning the
bootstrap race.

`WriterIdentityRegistry.revoke_identity(wallet, identity)` is available for a
future targeted admin API/CLI that removes a single identity from the set
without clearing the whole key (e.g. when more than one identity has been
intentionally authorized for the same wallet during a controlled migration
window).
