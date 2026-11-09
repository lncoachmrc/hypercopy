# TRAXION Strategy Lab — PnL observation telemetry v1

**Architecture:** additive migration 0019; read-only Hyperliquid market observations
already collected by the existing Profit Exit worker; PostgreSQL append-only
snapshots, SHADOW followers only. No new execution branch or live orders.

## Why

PR #302 improves the model's context only while net PnL is positive, but the
decision table does not persist failed/negative assessments. We cannot
measure the percentage of profitable positions that later fell below
break-even without recording those observations.

## Recorded fields and scope

At each eligible current source cycle evaluation (also for net <= 0), record:
user, epoch, provider, network, asset, source cycle and version, evaluation
slot, signed position, market mid, conservative exit estimate, entry,
estimated net PnL (nullable), completeness, funding model, position match,
eligible flag and abstention reason. Only internal user UUIDs are stored.
A deterministic per-slot UUID is used with PostgreSQL `ON CONFLICT DO NOTHING`.
Repeated workers cannot rewrite old observations.

**Important limitations:** PnL is an estimate, not a realized trade return.
SHADOW uses a hypothetical position and does not model funding. The sampled
peak is the maximum at observation timestamps, not the true exchange MFE.
A lack of new master positions means the table remains empty. No backfill
with invented prices; no comparison is statistically valid until a cohort
of genuine observations exists.

## Read-only measurement

For a preliminary per-position trajectory, query a single source cycle:

```sql
SELECT observed_at, asset, state_version, follower_position_size,
       mark_price, executable_exit_price, net_pnl, pnl_complete,
       complete, eligible, position_matches, reason
FROM ai_profit_exit_observations
WHERE user_id = :user_id
  AND execution_epoch_id = :execution_epoch_id
  AND asset = :asset
  AND source_cycle_id = :source_cycle_id
ORDER BY observed_at, evaluation_slot;
```

Do not compare PnL across differing versions/sizes; source reductions and
scale-ins reset the cost basis and may change the estimation methodology.
Deduplicate by ID; do not treat the number of evaluations as independent
trades. Join later cycle outcomes (including fees/slippage) only after
verification. Require a prospective holdout/walk-forward comparison before
changing any real exit policy.

## Implementation safety gates

- Migration 0019 is **additive**; downgrade drops the new research table
  (would destroy collected research and requires separate approval).
- Collection is only performed in `ProfitExitFeatureMode.SHADOW` and
  `CopyState.SHADOW`, after verified source cycle and market collector.
- Existing candidate eligibility is unchanged: negative, zero, incomplete
  or mismatched observations never enter the LLM decision branch.
- `ON`, Risk Engine, Hyperliquid execution and wallet authentication remain
  unchanged.
- Do not deploy/migrate staging or production without reviewing the correct
  Railway environment and service. No merge/mainnet authorization.

## Existing historical baseline — not a policy backtest

The supplied `10_master_events_history.csv` contains **1,766 master fills**,
**211 fully observed completed cycles** (102 LONG, 109 SHORT) and **42
left-censored cycles**. Their computed *gross price* PnL is about
**−53.61 USDC** (LONG +126.64, SHORT −180.25), excluding commissions,
funding, spread/slippage and follower divergences. It contains no
time-resolved intra-position mark/bid/ask series; hence it cannot quantify
the gain from any earlier exit or identify a profitable trailing threshold.
