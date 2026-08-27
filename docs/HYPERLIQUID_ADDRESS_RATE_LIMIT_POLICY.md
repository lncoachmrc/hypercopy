# TRAXION Hyperliquid address action rate-limit policy

## Scope

Hyperliquid enforces two independent classes of limits that matter to TRAXION:

1. a shared REST/IP weighted budget;
2. a per-user/address action budget.

`app.adapters.ratelimit.WeightedRateLimiter` remains the authority for the first
constraint. HF-005 adds observability and conservative backoff for the second.

## Hyperliquid address rules

The current official Hyperliquid rate-limit documentation states that address
limits apply to exchange actions, not `/info` reads. Each address receives an
initial action buffer of 10,000 requests and earns additional action capacity
from cumulative traded volume at one request per 1 USDC traded. Subaccounts are
accounted separately. A batch of `n` actions counts as `n` address requests.

Once an address is rate-limited, Hyperliquid still permits one request every
10 seconds. TRAXION mirrors that documented degraded cadence only after an
exchange throttle has actually been observed.

The read-only `userRateLimit` info request exposes the exchange's current fields:

- `cumVlm`;
- `nRequestsUsed`;
- `nRequestsCap`;
- `nRequestsSurplus`.

## TRAXION policy

TRAXION deliberately does **not** synthesize an authoritative local address cap.
An address can consume quota outside TRAXION and its exchange allowance depends
on cumulative venue volume, so a purely local counter cannot be authoritative.

Instead:

- every signed follower action attempt is counted in Redis by public follower
  account address and network;
- an observed 429/rate-limit response records a per-address throttle event;
- the affected address receives a Redis-backed 10-second backoff shared across
  TRAXION processes;
- other follower addresses are not delayed by that backoff;
- after an observed throttle, TRAXION best-effort reads `userRateLimit` and
  persists the exchange snapshot for diagnostics;
- an administrator can explicitly refresh the official snapshot through the
  read-only admin diagnostic endpoint;
- aggregate action/throttle/backoff counters are exported through `/metrics`
  without address labels, avoiding high-cardinality Prometheus series.

The diagnostic `/info` request is not polled on every order because its REST
weight would unnecessarily consume the shared IP budget on the trading hot path.

## Safety interaction with signed actions

Address observability never changes the ambiguity policy for a submitted signed
action. A 429 or transport error after submit can have an indeterminate external
result. The execution layer therefore continues to persist that attempt as
`UNKNOWN` and resolves exchange truth before any replacement action. HF-005 does
not blind-retry an old CLOID.

The local backoff delays only a *subsequent* signed action for that same account.
It is checked before acquiring the PostgreSQL signer lock, so a cooling account
does not occupy the distributed signer critical section while waiting.

## Leverage-update suppression

The audit suggested suppressing redundant actions such as leverage updates when
verified state already matches. Reconciliation already avoids creating a
leverage-only job when its fresh exchange snapshot matches the desired config.
For combined execution jobs, TRAXION still re-applies desired leverage immediately
before an exposure-changing order. That is a deterministic execution safety
precondition: relying on an older reconciliation snapshot could miss an external
or manual leverage change between reconciliation and submit.

HF-005 therefore does not remove that safety precondition. Further action
suppression would require a fresh ownership/freshness proof at submit time and is
outside this finding.

## Operations

Admin diagnostic:

`GET /api/v1/admin/users/{user_id}/hyperliquid-rate-limit`

It returns the follower network, public account address, the official
`userRateLimit` fields and the local Redis accounting/backoff snapshot. It does
not expose signing credentials.

Aggregate metrics:

- `hypercopy_hl_address_action_attempt_count`;
- `hypercopy_hl_address_throttle_count`;
- `hypercopy_hl_address_backoff_wait_count`.

An increase in `hypercopy_hl_address_throttle_count` should be treated as an
operator alert and investigated using the admin diagnostic before increasing
execution frequency or worker replicas.

## Explicit non-goals

- no real API stress test or deliberate quota exhaustion;
- no automatic purchase/reservation of additional Hyperliquid action capacity;
- no Railway replica change;
- no mainnet enablement;
- no change to the shared IP budget;
- no change to Risk Engine, CLOID, UNKNOWN recovery or order sizing.
