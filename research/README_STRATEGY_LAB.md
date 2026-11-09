# TRAXION Strategy Lab — Historical 1h OHLC Replay

**Research only, not an order engine.** Pure Python standard library, with no
exchange execution, account access, LLM calls, wallet secrets, or deployment
integration. No private trading-history CSV is committed to this repository.

## Purpose

Test whether hypothetical early **profit-only** exits based on a sampled
residual-position net-PnL high-water mark would have improved historical
master-cycle outcomes, versus faithfully copying the master's fills.

This is a *proxy for a decision policy*, not a replay of the actual LLM's
HOLD/CLOSE_PROFIT/ABSTAIN outputs. Profit Exit still uses a separate live
residual-fee/funding calculator; this research explicitly **does not model
funding**, actual maker/taker tiers, order-book depth or order failures.

## Inputs

- A locally held master fill CSV with columns:
  `event_ts,asset,side,size,price,start_position,position_after,master_equity`.
- A user-supplied gzip JSON of **public** Hyperliquid `candleSnapshot`
  one-hour OHLC prices: top-level `source="Hyperliquid public candleSnapshot"`,
  `interval="1h"`, and `markets={asset: [{t,o,h,l,c}]}`.

The Hyperliquid endpoint exposes only the most recent 5,000 candles per
interval. A 1-hour series covers about 208 days when available. **Never assume
an API response provides complete history**: coverage is validated per cycle.

The initial research collection obtained 75,897 hourly candles across 81
symbols from 2026-08-27 to 2026-10-05, via a temporary read-only GitHub
Actions workflow. The workflow was **removed from the final PR**. Public
market data was stored only in a 7-day CI artifact, not in Git history.

## Local repeatable analysis

```bash
python3 research/strategy_lab_replay.py \
  --fills /private/path/10_master_events_history.csv \
  --candles /private/path/public_market_candles_1h.json.gz \
  --out /private/path/strategy_results.json
```

`python -m unittest discover -s research -p test_strategy_lab_replay.py -v`
runs synthetic regression tests without network connectivity.

## Research method and safeguards

- Reconstruct every full cycle from signed fills, including scale-in, partial
  reduction and reversal. Exclude incomplete opening history. Equal-time
  sequencing is **inferred from position continuity**, not claimed as
  authoritative causal order; conduct sensitivity checks excluding these cycles.
- Both counterfactual and master baseline charge *assumed* 4.5bps fees and 3bps
  copying slippage per fill. A forced early exit charges another 5bps adverse
  exit slippage and 4.5bps fee by default. No funding reconstruction.
- Exit signal sees only an already closed 1h candle. Forced execution uses the
  **following candle's open**, not the candle's high/low and not an earlier
  timestamp. Never know future open while deciding.
- The profit-eligibility gate uses estimated **residual-position** economics
  (weighted entry, remaining entry-cost pool). Already-realized partial PnL
  cannot falsely make a losing residual eligible. The alternative's *total*
  cycle PnL retains actual previous partial realizations.
- Reset the peak after every master fill/state-version change, mirroring the
  strict comparability scope of Profit Retention v1.
- Explore 12 predeclared giveback/peak-return combinations. Pick using
  completed-before-2026-09-22 historical cycles; evaluate separately on
  initiated-after-2026-09-22 cycles. Flag overlapping cycles separately.
  **This cutoff was inspected in an earlier report**, therefore the later
  segment is not a pristine prospective holdout.
- Check fee/slippage sensitivity and split LONG/SHORT; never use the test
  segment to change a production trading decision.

## Interpretation

A strategy can show a positive *relative PnL improvement* and still have a
negative simulated total net result. Fee, funding, spread, latency, liquidity,
trade sizing and severe adverse fills can erase the hypothetical advantage.
Bootstrap confidence intervals from this small, correlated sample do not prove
a tradable edge.

**Do not promote any parameter into ON/mainnet without new prospective
SHADOW/testnet evidence, actual execution-cost accounting, CI, staging and
the required explicit authorization.**
