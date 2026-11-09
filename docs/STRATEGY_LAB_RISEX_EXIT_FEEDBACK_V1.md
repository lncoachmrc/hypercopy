# RISEx testnet SHADOW exits and contextual feedback v1

## Scope
Implementation based on integrated PR #305, commit 4a64146fafdd23b85f1241fad9e8f5e7facf12ff. No deployment, main merge, provider order or credentials change. No new migration beyond inherited 0019.

Hyperliquid master fills remain the input for both execution providers. Entry sizing, copying of master closes, risk limits and operational order paths are unchanged. Capital Intelligence execution influence is not enabled by this change. Mainnet RISEx and Profit Exit ON remain excluded from the new provider branch.

## RISEx evaluation
A separate read-only branch is invoked by the existing Profit Exit portfolio workflow. It selects active users whose copy state is SHADOW and whose active execution epoch/account is bound to RISEx testnet. It does not require a legacy Hyperliquid TradingAccount. It verifies the current source cycle, reads public RISEx market precision and bid/ask through the existing transport, then computes a hypothetical residual exit with adverse slippage and conservative tick rounding. Negative, zero and incomplete valuations are recorded before the positive-only decision gate.

`ENABLE_LIVE_TRADING` must be exactly `false`. `RISEX_SHADOW_TAKER_FEE_BPS` must be supplied explicitly to the AI worker service in the isolated testnet stack; it is a numeric, NON-SECRET paper-trading assumption, not a fee verified from a customer account. There is no default. Missing or invalid fees lead to incomplete observations and no model decision. No Railway variable has been configured by this PR.

Entry price is a Hyperliquid master cross-venue proxy, not an actual RISEx fill. Funding is unknown (`residual_funding=null`, `pnl_complete=false`), never silently zero. The estimate is tagged `risex_copy_shadow_estimate`, with the fee assumption encoded in its funding/cost-model label. Bid/ask depth is not sufficient to prove full execution of the hypothetical position. These economics MUST NOT authorize operational closes.

The shared LLM contract remains HOLD, CLOSE_PROFIT, ABSTAIN. Every result is record-only; the new branch never creates a CopyJob, signs or posts an order. After the model returns, a mode/destination change causes the decision to be discarded. Per-user failures are isolated and counted. Master close copying is neither intercepted nor deferred.

## Learning mechanism implemented
This is online contextual evidence, NOT automatic neural weight training or an autonomous strategy promoter. Both providers' SHADOW SHORT decision payloads now include versioned feedback built from prior comparable cycles. The payload and evidence hash are persisted alongside the decision, allowing later inspection of exactly what the model saw.

For each prior cycle, the earliest eligible decision is paired with the first complete, same-position observation 30 to 35 minutes later. A positive normalized difference means the earlier hypothetical close estimate exceeded the later hold estimate. This is a **sampled short-horizon counterfactual**, not realized or final-cycle profit. It evaluates the choice available at a decision, not the performance of an executed CLOSE_PROFIT order.

The query isolates user, epoch, provider, network, asset, direction, valuation basis, PnL completeness and configured cost model. Pairing also fixes source cycle/version, position size and entry price. It excludes the current cycle, future outcomes, operational intent/job rows, ABSTAIN rows and legacy decisions without explicit SHADOW/version provenance. It includes both favorable and unfavorable differences. One cycle contributes at most one pair; at most 100 recent cycles are used.

The summary contains sample count, mean difference in basis points, counts favoring close/hold, a Beta(1,1)-smoothed win proportion (not a calibrated success probability), and a SHA-256 evidence identifier. The 20-cycle warm-up floor is an engineering guard, NOT proof of statistical significance or profitability. The prompt only receives the feedback-specific instruction when the summary is mature; warm-up evidence is labeled as insufficient. No threshold is converted automatically into an order rule, and no feedback is attached to ON decisions.

## Limitations / work remaining
- Source cycles closing or changing size before the sampled horizon may have no comparable pair: missing evidence is not a win or a loss. This is a selected cohort; no causal-performance or profitability claim follows.
- Final-cycle outcome attribution, continuously marked shadow portfolios, actual follower entry/fee/funding history, out-of-sample policy evaluation and version promotion/rollback remain separate requirements before operational learning.
- Per-asset/per-user segmentation can take substantial observation time to mature. The old historical CSV is not silently imported as training evidence.
- No automatic activation, live adaptive risk, or arbitrary code/prompt rewrites.
- Real-account RISEx exit execution requires exact economics and its existing signed-execution gates. This PR does not implement that live close path.

## Verification plan
RED: new feedback and RISEx quote tests must fail against the baseline while its suite stays green. GREEN: same tests plus runtime fakes at network/DB boundaries, PostgreSQL pairing/isolation and candidate query on the migrated database. Full backend, lint/type/compile, CI, CodeQL and existing safety tests must pass. A passing fake-provider test is not a real wallet or exchange test.

## Release
Keep this PR draft. No main merge, mainnet or deployment without the separate authorization and deployment prerequisites. Production remains unchanged. Use the existing isolated RISEx testnet stack, not a new staging design.
