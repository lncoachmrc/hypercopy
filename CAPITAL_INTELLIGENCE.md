# Traxion Capital Intelligence

## Purpose

Capital Intelligence improves master-to-follower portfolio fidelity when follower capital is too small to reproduce every master leg above Hyperliquid's minimum order notional.

The architecture deliberately separates **reasoning** from **trading authority**:

1. the deterministic optimizer builds a small set of bounded candidate policies;
2. the LLM supervisor may select only one of those candidate IDs;
3. current master state is reapplied to the selected structural policy at execution time;
4. the existing Risk Engine, position-targeting engine, leverage rules, Cloid/idempotency and mainnet gates remain authoritative;
5. only the existing execution worker can sign and submit exchange actions.

The LLM never receives a Hyperliquid private key, agent key, KMS plaintext, session secret, Stripe secret, database password or Redis password.

## Master strategy learning

Every intelligence refresh learns from persisted `master_events` inside `LLM_STRATEGY_WINDOW_DAYS` (default 30 days). The deterministic profile includes:

- openings and closes;
- scale-ins and scale-outs;
- reversals;
- fill frequency and median event interval;
- average fill notional and fill/equity ratio;
- observed holding duration;
- micro-fill ratio;
- per-asset persistence score.

The profile becomes more representative as history accumulates. The LLM receives only compact aggregate features and deterministic candidate metrics.

## Provider failover

Supported providers:

- OpenAI
- Anthropic
- DeepSeek

`LLM_PREFERRED_MODEL` is attempted first when it matches one of the configured provider models. Remaining configured providers follow `LLM_PROVIDER_ORDER`.

The router automatically advances to the next provider after a timeout, rate/credit error, provider 5xx, authentication problem, malformed JSON or an invalid/nonexistent candidate ID.

When all providers are unavailable:

- `shadow`: the deterministic optimizer continues and the failure is visible in the decision status;
- `active`: Traxion persists and applies the deterministic **Exact Ratio** fail-safe policy.

## Candidate policies

- **Exact Ratio** — existing proportional position-targeting behavior.
- **Smart Fidelity** — compresses sub-minimum legs with a 5% capital buffer.
- **Smart Balanced** — 10% buffer.
- **Smart Defensive** — 15% buffer.

Smart policies choose a structural set of participating assets and a bounded allocation scale. They do **not** freeze position direction or size. Current master exposure is reapplied on every realtime event and reconciliation cycle, so a master close becomes zero immediately and a reversal changes sign immediately even between LLM refreshes.

## Railway variables

### execution-worker — secret + non-secret intelligence variables

```env
LLM_ENABLED=true
LLM_CAPITAL_MODE=shadow
LLM_PROVIDER_ORDER=openai,anthropic,deepseek
LLM_PREFERRED_MODEL=gpt-5.6-terra

OPENAI_API_KEY=<secret>
OPENAI_MODEL=gpt-5.6-terra

ANTHROPIC_API_KEY=<secret>
ANTHROPIC_MODEL=claude-sonnet-5

DEEPSEEK_API_KEY=<secret>
DEEPSEEK_MODEL=deepseek-v4-pro

LLM_TIMEOUT_SECONDS=12
LLM_MAX_OUTPUT_TOKENS=700
LLM_ANALYSIS_INTERVAL_SECONDS=300
LLM_STRATEGY_WINDOW_DAYS=30
LLM_RECOMMENDED_COVERAGE_PCT=90
LLM_DECISION_MAX_AGE_SECONDS=900
```

Provider API keys belong only on `execution-worker`.

### api — non-secret variables only

The API needs the public configuration values so the authenticated frontend can display the configured preference consistently:

```env
LLM_ENABLED=true
LLM_CAPITAL_MODE=shadow
LLM_PROVIDER_ORDER=openai,anthropic,deepseek
LLM_PREFERRED_MODEL=gpt-5.6-terra
OPENAI_MODEL=gpt-5.6-terra
ANTHROPIC_MODEL=claude-sonnet-5
DEEPSEEK_MODEL=deepseek-v4-pro
LLM_ANALYSIS_INTERVAL_SECONDS=300
LLM_STRATEGY_WINDOW_DAYS=30
LLM_RECOMMENDED_COVERAGE_PCT=90
LLM_DECISION_MAX_AGE_SECONDS=900
```

Do not place `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` or `DEEPSEEK_API_KEY` on the frontend service.

### master-watcher / frontend

No LLM provider secret is required.

## Rollout

1. Deploy migration `0008_capital_intelligence`.
2. Set provider secrets on `execution-worker`.
3. Set `LLM_ENABLED=true`, `LLM_CAPITAL_MODE=shadow`.
4. Observe candidate selection, coverage, recommended capital, failover behavior and learned master profile in the dashboard.
5. Compare shadow recommendations against Exact Ratio on TESTNET.
6. Only after validation set `LLM_CAPITAL_MODE=active` on both `execution-worker` and `api`.
7. Keep follower mainnet disabled until the project's existing mainnet Definition of Done is independently satisfied.

Changing from `shadow` to `active` never bypasses risk or live-trading gates. A stale intelligence decision, changed risk multiplier/minimum notional or changed network topology causes automatic fallback to the original Exact Ratio algorithm.
