# TRAXION — Profit Retention v1 (SHADOW-only)

**Status:** experimental, record-only; no mainnet activation or promised profitability.
**Source of truth:** `docs/SPEC.md`, current Profit Exit economics, existing Risk Engine.
**Change:** give AI Profit Exit an auditable series of *sampled* positive net-PnL observations.

## Problem and hypothesis

Before this change the AI sees the current economics but cannot distinguish a
fresh profit peak from a position giving back previously observed profits.
Hypothesis H1: seeing prior PnL high-water marks and recent changes can improve
CLOSE_PROFIT vs HOLD judgment, relative to the prior prompt, once evaluated
out-of-sample. This is an **untested strategy hypothesis**, not a measured edge.

## Behavior

- Only when the **global Profit Exit mode is SHADOW** and the existing economic
  gate has certified a *strictly positive* current estimated net PnL, load prior
  durable decisions in the same user/epoch/provider/network/asset/source cycle,
  causal state version, follower position size and accounting-completeness class.
- A single indexed SQL window query yields the latest sample, prior peak and
  sample count. Compute current peak, observed giveback and recent PnL change.
  The LLM sees this context and a SHADOW-specific instruction to consider profit
  erosion without imposing a fixed retracement, take-profit or time threshold.
- Add the experiment identifier `profit_retention_shadow_v1` to the persisted
  decision inputs. The existing idempotent decision record captures the entire
  contemporaneous input and action.
- **No change in ON or OFF**. The ON path receives the original prompt and
  decision inputs, and all existing order/risk/safety checks remain unchanged.
  SHADOW never emits a CopyJob as an operational Profit Exit.
- No new environment variables, migrations, wallet permissions, model calls,
  execution side effects or API endpoints.

## Measurement interpretation

The high-water mark is the peak of **discrete positive AI evaluation samples**
stored in PostgreSQL. It is *not* exchange MFE or the highest market value between
polls. For the very first sample the peak equals the current net PnL; comparisons
are unavailable. SHADOW copy estimates omit funding and are explicitly flagged
`funding_complete=false`; real-account economics can be complete.

Example: prior sampled peak 10 USDC; prior latest 7; current 6:
`observed_giveback_usdc=4`, `observed_giveback_pct=40.00`,
`change_since_previous_usdc=-1`. This is *evidence for the model*, not an
automatic close order or evidence that a 40% threshold is profitable.

The exported master-fill CSV contains 1,766 fill records but no intraperiod
quotes, actual follower fee/funding series, or evaluated peak PnL trajectory.
It **cannot** demonstrate a net performance uplift or establish a defensible
profit-preservation threshold.

## Acceptance and future measurement

1. Tests: first observation, new peak, erosion, non-finite/negative exclusion,
   precise cycle/epoch scope, no ON-mode prompt change, existing action contract.
2. Before claiming uplift: compare profit-retention v1 against baseline on
   prospective SHADOW decisions with genuine open positions and completed
   market-data histories, accounting for costs, LLM expenses, false exits and
   adverse selection. Use out-of-sample and positive/negative regimes.
3. Observe per-position maximum sampled net PnL, giveback, CLOSE_PROFIT/HOLD
   decisions, later source-cycle outcome and relative performance. Ineligible
   or negative-PnL observations are not yet persisted: a dedicated observation
   telemetry step is required to quantify deterioration through zero.
4. Staging/testnet and runtime checks precede any operational rollout;
   production and mainnet require explicit authorization under SPEC.md.

**Deployment:** PR only; no merge, no Railway change, no Hyperliquid orders.
