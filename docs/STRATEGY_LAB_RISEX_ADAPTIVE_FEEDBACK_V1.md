# TRAXION — Multi-provider AI Profit Exit + Adaptive Feedback v1

**State:** review-only; feature gated to global `ai:profit_exit_mode=SHADOW`.
**Base:** #305 (Capital Intelligence fixes, Profit Retention and immutable 0019 observation telemetry).
**Source of truth:** docs/SPEC.md, existing Risk Engine and ADR-0004/0006.

## Architecture / precedence

1. **Source:** Watch one Hyperliquid master via read-only public account/position and confirmed fills. Persist source cycles and distribute source events through existing CopyJobs. The copy fanout for both Hyperliquid and RISEx is not modified by this change.
2. **Regular follower reductions:** Deterministic master-copy path remains authoritative. AI cannot block a master close, create or increase a position, change leverage, increase margin, or reverse the position.
3. **Optional early profit exit:** The existing Profit Exit decision engine remains responsible for HOLD/CLOSE_PROFIT/ABSTAIN. Its operational (ON) exit path remains **Hyperliquid-only**, unchanged; RISEx returns research observations only. The same-cycle retarget fence remains intact.
4. **RISEx read-only pilot:** A verified `execution_provider=risex`, network=testnet and `CopyState.SHADOW` follower is read using the dedicated RISExTradingAccount, bound to a matching active ExecutionEpoch. **Do not use the legacy TradingAccount as proof of a RISEx account.** A fresh uncredentialed `RISExReadOnlyHTTPTransport` only calls `GET /v1/markets` and `GET /v1/orderbook`. No signed transport, bearer token, RPC write, credential decryption, submission or CopyJob is possible from this branch.
5. **Economics disclaimer:** RISEx SHORT/LONG estimate uses its own best bid/ask and an outward-rounded hypothetical IOC exit with risk-profile slippage; the Hyperliquid master's entry as a *proxy*, and published tier-1 taker fee 0.03% as an *assumption*. It does **not** account for the RISEx follower's actual entry price/fills, effective fee tier, builder fees, or funding. `pnl_complete=false`, `funding_model=not_modeled`, `evaluation_basis=risex_shadow_estimate`. These are not verified real profits and **must never authorize a real exit**.

## Online e-learning = causal feedback, not autonomous retraining

- Existing `ai_profit_exit_decisions` and `ai_profit_exit_observations` are the durable data source. For a past SHORT shadow evaluation, pair with the *next* complete, position-matched observation **after** the decision, on the same user, epoch, provider, network, asset, source cycle/version, follower size and completeness class, within 2h. Ignore samples in the current master cycle.
- **What is learned:** whether waiting one more observation improved/deteriorated the *estimated exit PnL*. CLOSE is favorable if the next estimate decreases; HOLD if the next estimate increases. Normalize by entry notional to bps. These are short-horizon proxy labels, **not realized PnL**, exchange fills or causal A/B comparisons.
- Count **at most one decision pair per source master cycle**, so 10 users copying the same master movement do not become 10 independent observations. Split chronologically ~70% earlier / 30% later. Publish only descriptive later-period evidence with at least 20 independent cycles and at least 8 later cycles. Otherwise return `status=collecting` without a directional hint.
- Segregate by provider, network, asset and SHORT. No cross-exchange learning transfer, no LONG benefit claim. Aggregate statistics contain no wallet addresses, user IDs or individual results.
- Future evaluations in **SHADOW SHORT only** receive versioned aggregate feedback. This is a continuously updating *advisory learning context* for the LLM; it cannot alter deterministic safety checks, execution policy, model weights, or rewrite prompts.
- There is **NO automatic promotion** into ON or mainnet. For real improvement evidence, subsequent validation must use verified exchange fills, funding, slippage, independent source cycles, repeated regimes, and a prospective untouched holdout. A positive statistical association does not guarantee higher profits.

## Known gaps and acceptance

- The current testnet RISEx account may lack a verified funded position, trade history and fee history; the pilot uses a proxy entry. A future adapter for real RISEx execution requires true per-user fill/funding accounting, deterministic reduce-only and pre-post reconciliation under ADR-0004/0006. Neither is provided by this change.
- The `risex-e2e` Railway stack was last seen offline, and the latest api-test deploy failed; nothing here starts or changes it. No production deployment or mainnet order.
- CI gate: unchanged full backend suite, migration `0019`, target tests, CodeQL, Gitleaks, and built-in frontend/landing audits. Frontend's Zod exception remains time-bound on #305; test-only fee assumption is explicit.
- Manual integration: prove master source on Hyperliquid, RISEx testnet market/book, fresh execution epoch identity, SHADOW observation persistence and feedback update after new source cycles; confirm **zero RISEx writes** on this path.
- Merge to main, production deployment, mainnet trading and any irreversible changes require separate explicit authorization.
