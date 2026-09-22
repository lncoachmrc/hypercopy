# ADR-0006 — RISEx mainnet enablement gate

- **Status:** Proposed
- **Date:** 2026-09-20
- **Scope:** TRAXION / RISEx production-mainnet enablement, residual-risk acceptance and controlled rollout
- **Decision owner:** TRAXION project owner
- **Related decisions:** ADR-0002, ADR-0003, ADR-0004, ADR-0005
- **Related specifications:** RISEx follower integration design; RISEx negative replay probe design
- **Acceptance rule:** this ADR may transition from `Proposed` to `Accepted` only in the Pull Request that actually introduces the final RISEx mainnet enablement gate. Implementation work before that PR does not constitute acceptance.
- **Transitional-risk review date:** 2026-10-20

## Context

TRAXION is adding RISEx as a second execution provider while preserving Hyperliquid as the master/source venue.

The intended runtime path is:

`Hyperliquid master → TRAXION intent → deterministic Risk Engine → execution_provider → Hyperliquid or RISEx`.

The current RISEx work has established significant testnet foundations: provider routing, signed-order preparation components, deployment pinning, session-key analysis, replay architecture, point-of-use freshness controls and an isolated negative replay probe.

Those controls were developed under a deliberately bounded testnet risk model.

RISEx mainnet changes the risk perimeter materially.

A failure on testnet affects disposable/faucet capital. A failure on mainnet can affect real user capital. Consequently, conclusions accepted for testnet must not automatically authorize production-mainnet execution.

This ADR defines the conditions that must all be satisfied before TRAXION may enable:

`execution_provider = risex`

and:

`execution_network = mainnet`.

The gate is fail-closed.

No individual successful test, CI run, provider response, operator assertion or previously accepted testnet ADR can independently authorize RISEx mainnet.

## Decision

RISEx mainnet remains **BLOCKED** until every mandatory condition in this ADR is satisfied and attached as reviewable evidence to the mainnet-enablement PR.

The implementation sequence is:

`4B-bis → 4C → behavioral security evidence → PR C → ADR-0006 final acceptance → mainnet enablement`.

PR C may expose the provider-selection UI only when its production behavior remains gated by this ADR. The existence of a RISEx option in code or UI is not authorization to place mainnet orders.

## 0. Per-user RISEx account and signer isolation is mandatory

This section establishes a mandatory gate that is independent of, and in addition to, the collateral-confinement and kill-switch controls defined in §1. It must be satisfied before ADR-0006 can move from `Proposed` to `Accepted`.

A code audit of the current implementation found that RISEx testnet execution relies on a single service-wide environment signer and account (`RISEX_TESTNET_SIGNER_PRIVATE_KEY`, `RISEX_TESTNET_ACCOUNT_ADDRESS`), and that `PUT /trading-provider` creates an `ExecutionEpoch` with `account_address` and `credential_version` set to `NULL`. Two distinct users who both switch their execution provider to RISEx therefore receive an identical `NULL` binding: there is no per-user account or signer isolation. This is incompatible with production-mainnet execution of real user capital and must be resolved before this ADR is accepted.

The following requirements are mandatory:

1. There must be exactly one verified RISEx account identity and one dedicated RISEx session/API signer per TRAXION production user. Credentials must not be shared across users.
2. The user creates and registers their own RISEx signer externally and supplies their account address plus a dedicated signer private key to TRAXION. TRAXION does not mint credentials on the user's behalf.
3. TRAXION must not generate a signer for the user and must not perform `RegisterSigner` on the user's behalf.
4. TRAXION must verify the authoritative, on-chain account-to-signer binding, its active state, its expiry, and its required permissions before relying on it. TRAXION must fail closed on any mismatch, unknown state, or indeterminate verification result.
5. The signer private key must be envelope-encrypted at rest. It must never be stored or handled in plaintext.
6. The active `ExecutionEpoch` must be bound to a specific account address and credential version. `NULL` is not an acceptable value for either field in production.
7. The `execution-worker` must resolve the credential to use by `CopyJob.user_id` together with the active epoch, decrypting the signer only at the point of use, and must validate the account/signer binding at that point of use.
8. A shared, environment-level signer or account used across multiple production users is explicitly prohibited.
9. `RISEX_TESTNET_ACCOUNT_ADDRESS` and `RISEX_TESTNET_SIGNER_PRIVATE_KEY` are permitted only for isolated, single-account test verification. They must never be used as, or generalized into, the production credential model.
10. Rotation, revocation, expiry, a provider switch, or any detected binding mismatch must invalidate the old epoch and cause TRAXION to fail closed until a fresh, verified binding is established.
11. Acceptance evidence for this ADR must include at least two distinct users, demonstrating that no credential is reused across users. This gate is mandatory before ADR-0006 can move from `Proposed` to `Accepted`.

## 1. ADR-0002 must be explicitly re-evaluated for real capital

ADR-0002 accepted the observed RISEx testnet signer permission state despite:

`hasPermission(MoveFund) = true`

because no documented session-key-authorized fund-movement path had been identified and the amount economically exposed during testnet verification was deliberately bounded.

That conclusion is insufficient by itself for production mainnet.

For mainnet, TRAXION distinguishes three materially different ways of controlling the risk created by a signer that exposes `MoveFund`.

They are not equivalent.

The preferred order is:

1. **(a) permission reduction / least privilege;**
2. **(b) cryptographically enforceable isolation boundary;**
3. **(c) collateral confinement as a temporary economic mitigation.**

Mainnet may proceed under option (c) only within the explicit quantitative and operational limits defined below.

### 1.1 (a) Permission reduction — preferred

The preferred production state is a signer whose on-chain permissions are restricted to the operations TRAXION actually requires.

At minimum:

- `Perps = true`;
- `MoveFund = false`;
- unrelated permissions not required by TRAXION are disabled;
- the reduced scope is observable on-chain;
- account/signer binding is verified;
- the scope is re-checked during readiness and point-of-use freshness validation.

TRAXION has identified `enablePermission` and `disablePermission` in the Authorization interface, but the exact supported EIP-712 signing model required to use them has not yet been authoritatively established.

TRAXION must not invent or reverse-engineer a production signing payload and treat it as provider-supported behavior.

Therefore option (a) is currently blocked on authoritative RISEx documentation or an equivalent supported reduced-scope signer-registration flow.

As soon as RISEx provides a verified supported mechanism, option (a) becomes the required target state.

### 1.2 (b) Cryptographically enforceable isolation boundary

If direct permission reduction is unavailable, a second acceptable long-term architecture is an independently enforceable account boundary such as a `BaseSubAccount`-style contract or equivalent construction.

Such a boundary must ensure cryptographically that possession or compromise of the TRAXION trading signer cannot authorize fund movement outside the deliberately isolated trading account or subaccount.

The boundary must be enforceable by contract/account semantics, not by:

- TRAXION application policy;
- operator convention;
- a UI warning;
- an assumed provider behavior;
- the user's promise to deposit only a certain amount.

No production-ready RISEx boundary satisfying this requirement has yet been established for the current TRAXION integration.

Therefore option (b) is not currently available.

### 1.3 (c) Collateral confinement — temporary economic mitigation

While neither (a) nor (b) is available, TRAXION may temporarily bound the residual `MoveFund` risk by limiting the economic value intentionally accepted inside RISEx accounts used with TRAXION.

This is explicitly an **economic mitigation**.

It is not least privilege.

It is not a cryptographic security boundary.

It does not prevent a compromised signer from exercising whatever authority RISEx actually grants to that signer.

Its purpose is to establish a quantified, monitored loss budget for the transitional production phase.

#### 1.3.1 Exposure-at-risk definition

For each RISEx user account, the authoritative option-(c) capital-at-risk measure is:

```text
exposure_at_risk = summary.total_account_value
```

where `summary.total_account_value` is read from the live RISEx portfolio/account response.

This definition replaces the earlier draft formula:

```text
deposited_collateral + liquidatable_value_of_open_positions
```

because that formula double-counts cross-margin exposure. In a cross-margin account the open-position notional is already supported by the same account collateral/equity; adding position notional or liquidation value to the account capital a second time overstates the economic value actually present inside the RISEx account.

The correction is based on the live testnet `/v1/portfolio/details` response verified on 2026-09-21, which exposes `summary.total_account_value` together with collateral/balance fields, `summary.total_notional`, and per-position size/mark/liquidation data.

`summary.total_notional` is retained as a secondary leverage and gross-position signal. It is **not** an additive component of `exposure_at_risk`.

Position-level `size`, `mark_price`, `avg_entry_price`, `market_id` and `liquidation_price` remain relevant to leverage, liquidation-distance, reconciliation and deterministic Risk Engine controls, but they are not added to `summary.total_account_value` for the option-(c) loss budget.

If `summary.total_account_value` is unavailable, cannot be parsed as a finite numeric value, or is otherwise indeterminate, `exposure_at_risk` is **UNKNOWN** and the account fails closed for new exposure.

Missing or malformed data must never be interpreted as zero.

#### 1.3.2 Accepted loss budget and ceilings

Option (c) is based on the amount of real capital TRAXION is prepared to have economically exposed under the unresolved signer-authority model, not on an estimate of what users are expected to deposit.

The initial accepted observed exposure budget is:

```text
RISEX_USER_EXPOSURE_CEILING_USDC = 25,000
RISEX_TOTAL_EXPOSURE_CEILING_USDC = 75,000
```

The per-user ceiling limits concentration: compromise or misuse associated with one account should not intentionally place more than 25,000 USDC of observed capital-at-risk inside the transitional perimeter.

The aggregate ceiling is the project-level loss budget for option (c): TRAXION accepts at most 75,000 USDC of observed aggregate capital-at-risk while relying on collateral confinement rather than cryptographic least privilege. The aggregate value is derived from the initial invited-rollout ceiling of three active RISEx users at up to 25,000 USDC observed exposure each.

These values are a deliberate worst-case loss-budget decision, not an estimate of what users are expected to deposit.

They are policy ceilings over **observed** exposure. Because users can independently deposit capital and TRAXION is non-custodial, they are not cryptographically guaranteed maximum-loss bounds. The detection gap and provider-read outage limitation are recorded explicitly below.

The aggregate ceiling must not increase automatically because more users request RISEx access.

Any increase requires an explicit ADR amendment based on new evidence and a new loss-budget decision.

#### 1.3.3 Who verifies exposure and how often

The RISEx exposure monitor is a mandatory production component for option (c).

The `execution-worker` operational layer, or a dedicated component sharing the same authoritative database and provider-read model, must collect current RISEx exposure for every mainnet account enabled for TRAXION execution.

Required cadence:

```text
RISEX_EXPOSURE_MONITOR_INTERVAL_SECONDS = 240
RISEX_EXPOSURE_READ_TIMEOUT_SECONDS = 60
RISEX_EXPOSURE_SAMPLE_MAX_AGE_SECONDS = 300
```

Exposure must also be checked synchronously immediately before every candidate operation capable of increasing exposure.

The periodic monitor and the pre-POST check serve different purposes:

- the periodic monitor detects out-of-band changes such as a user depositing additional collateral;
- the pre-POST check prevents TRAXION from intentionally adding exposure when the latest verified state is already at or near the ceiling.

Cached account exposure older than `RISEX_EXPOSURE_SAMPLE_MAX_AGE_SECONDS` cannot authorize an exposure-increasing POST.

#### 1.3.4 Enforcement on threshold breach

If a user's verified:

`exposure_at_risk > 25,000 USDC`

TRAXION must:

- block new positions for that account;
- block all position increases for that account;
- preserve only explicitly reviewed risk-reducing/recovery actions;
- create an auditable security/operational incident;
- surface an operator alert;
- mark the account outside the ADR-0006 confinement envelope;
- require verified exposure to return below the ceiling before ordinary RISEx execution can resume.

TRAXION must not automatically withdraw or transfer user collateral.

If verified aggregate exposure across all active RISEx accounts exceeds:

`75,000 USDC`

option (c) is no longer within its accepted loss budget.

TRAXION must:

- block RISEx exposure-increasing writes globally;
- create a high-priority operational/security incident;
- keep risk-reducing recovery operations separately controlled;
- refuse automatic re-enable;
- require aggregate verified exposure to return to or below the ceiling, or require option (a) or (b), before normal RISEx execution resumes.

Above 75,000 USDC aggregate exposure, option (c) cannot be used as the basis for continued scale-out.

#### 1.3.5 Pre-POST exposure check

Before every exposure-increasing RISEx provider POST, TRAXION must establish from sufficiently fresh provider truth:

```text
current_user_exposure_at_risk <= 25,000 USDC
```

and:

```text
current_aggregate_exposure_at_risk <= 75,000 USDC
```

where every account-level value is derived exclusively from `summary.total_account_value`.

The candidate order's notional is **not** added to `exposure_at_risk`. Doing so would reintroduce the cross-margin double counting corrected in §1.3.1.

Order notional, leverage, margin headroom, liquidation distance, per-trade limits and asset exposure remain independently enforced by the deterministic Risk Engine and provider-specific execution constraints. `summary.total_notional` may be used as a secondary leverage/gross-position signal for those controls, but not as part of the option-(c) capital-at-risk sum.

If either the per-user or aggregate `summary.total_account_value` calculation cannot be completed from sufficiently fresh authoritative provider data:

`POST = BLOCKED`.

A post-trade monitor does not replace this point-of-use check.

#### 1.3.6 Atomic pending-execution reservation and accounting serialization

The option-(c) loss budget is governed by authoritative `summary.total_account_value` as defined in §1.3.1.

A pending order's notional or local `reserved_exposure_usdc` must **not** be added to `summary.total_account_value` when evaluating the 25,000 / 75,000 USDC option-(c) ceilings.

TRAXION nevertheless requires a durable pending-execution reservation for a separate reason: concurrent or ambiguous executions must not create a local risk/notional-accounting gap, permit blind resubmission, or allow unresolved provider effects to disappear from the execution budget.

The shared serialization domain remains the PostgreSQL transaction-scoped advisory-lock pattern already used by ADR-0004 control fencing, applied to a stable global resource key for RISEx execution accounting, conceptually:

`risex:mainnet:exposure-budget`.

The singleton execution-worker invariant in ADR-0004 is not considered a correctness guarantee for this control. The reservation mechanism must remain correct if multiple worker processes are active concurrently.

For every exposure-increasing RISEx mainnet order, the execution-accounting sequence is:

1. begin a database transaction;
2. acquire the transaction-scoped PostgreSQL advisory lock;
3. re-read sufficiently fresh authoritative `summary.total_account_value` for the candidate user and aggregate RISEx population;
4. fail closed if the option-(c) per-user or aggregate ceiling is already exceeded or indeterminate;
5. load active unresolved RISEx execution reservations relevant to deterministic order/risk headroom;
6. apply the ordinary Risk Engine and provider-specific notional/leverage controls without adding those reservations to `summary.total_account_value`;
7. create a durable reservation bound uniquely to the candidate `Execution`;
8. commit the reservation before any RISEx provider POST;
9. only after the commit may the existing continuous-window / freshness submission path reach the provider POST.

The durable reservation must contain enough information to preserve the unresolved execution obligation across restart and reconciliation. At minimum this includes:

- `execution_id` with a uniqueness guarantee;
- `user_id`;
- `execution_provider = 'risex'`;
- execution network;
- provider/client order identity;
- the conservative order/risk amount reserved for local execution accounting;
- reservation state;
- `created_at`;
- release/resolution evidence when eventually resolved.

The advisory lock does not need to remain held during the network POST. The committed reservation carries the serialized execution-accounting obligation across that external side effect.

Reservation release is fail-closed:

- a definitive provider rejection that proves no order/exposure side effect was created may release its reservation under the shared serialization lock;
- a definitive fill, partial fill or cancellation may release or reduce the reservation only inside a transaction holding the same serialization lock that also refreshes and persists the relevant provider truth, including `summary.total_account_value` and the position/notional state needed by the Risk Engine, so the real provider state replaces the pending reservation without an accounting gap;
- a provider acknowledgement that does not establish final execution state does not release the reservation;
- timeout, lost connection, malformed response, `SUBMITTING`, `UNKNOWN`, worker crash or any other ambiguous outcome does **not** release the reservation;
- reservations have no automatic TTL-based release.

After a crash between reservation commit and POST, or whenever transmission is uncertain, 4C must resolve the durable provider identity/nonce evidence before the reservation can be released. Until then the execution remains unresolved and blocks additional risk where required by the deterministic execution controls.

If PostgreSQL is unavailable, the advisory lock cannot be acquired, active reservations cannot be read, authoritative `summary.total_account_value` is stale/unavailable, or reservation state cannot be established unambiguously:

`POST = BLOCKED`.

The acceptance tests must include at minimum:

- reservation persisted before provider POST;
- concurrent execution-accounting authorization remains serialized across sessions/processes;
- option-(c) ceilings use only authoritative `summary.total_account_value` and do not add order notional or reservations;
- ambiguous provider outcome retains the reservation;
- process crash after reservation commit retains the reservation;
- definitive no-effect rejection releases the reservation only under the shared lock;
- filled/partially-filled resolution cannot create a gap between reservation release and refreshed provider/account/position truth;
- lock acquisition/database/provider-state failure produces zero provider POSTs.

#### 1.3.7 TRAXION does not control out-of-band deposits

TRAXION can enforce whether TRAXION itself submits another order.

TRAXION cannot, under the current non-custodial architecture, prevent an account owner from independently depositing additional collateral into RISEx.

Therefore option (c) is **detective plus reactive**, not a preventive custody boundary.

If the user independently deposits above the configured limit, the system has not prevented the exposure increase. It can only detect it on the next successful provider observation and then block further TRAXION exposure-increasing activity.

The ceiling must never be described as a guaranteed cap on the amount physically present in the RISEx account.

#### 1.3.8 Quantified detection gap

Under healthy provider telemetry, an out-of-band collateral increase can occur immediately after a successful sample.

With:

```text
monitor interval = 240 seconds
provider read timeout = 60 seconds
```

the accepted maximum healthy-telemetry detection window is:

```text
300 seconds = 5 minutes
```

from an out-of-band change occurring immediately after a successful observation to completion of the next scheduled observation, assuming the provider read completes within the configured timeout.

For up to that five-minute window, actual exposure can exceed the declared ceiling without TRAXION knowing it.

That five-minute detection gap is explicitly accepted as part of temporary option (c).

If RISEx account-state reads fail, the actual amount of an out-of-band deposit cannot be bounded by TRAXION until provider visibility returns.

After the last successful sample becomes older than:

`300 seconds`

TRAXION must fail closed for every exposure-increasing RISEx write and raise an incident/alert.

This stale-data hard stop prevents TRAXION from adding further exposure, but it cannot retroactively prevent or measure external deposits made during the provider-read outage.

Accordingly, during a provider-read outage the duration of **unknown external exposure is not cryptographically bounded**. This limitation is an explicit residual risk of option (c) and is another reason option (c) is transitional rather than equivalent to (a) or (b).

#### 1.3.9 Residual-risk statement

Under option (c), TRAXION explicitly accepts the following residual-risk model:

> If an attacker obtains effective control of a RISEx signer that still possesses `MoveFund` authority, the capital economically exposed inside the affected RISEx account is measured by the account's current `summary.total_account_value`, subject to the actual capabilities enforced by RISEx.

Gross position notional may exceed account equity because leverage can be used, but that notional is not added a second time to the option-(c) capital-at-risk measure. `summary.total_notional` remains a separate leverage/gross-position risk signal.

For multiple compromised accounts, the observed aggregate option-(c) exposure is the sum of their authoritative `summary.total_account_value` values.

Under normal monitored operation TRAXION intends to keep observed exposure within 25,000 USDC per user and 75,000 USDC aggregate.

Those figures are the accepted transitional loss budget, not a cryptographic guarantee that loss cannot exceed them.

An out-of-band user deposit can temporarily exceed the ceiling before detection, and a provider-read outage can make that excess unobservable until reads recover.

Collateral confinement limits accepted economic exposure operationally.

It does not prove that the signer lacks fund-movement authority.

It does not prevent misuse of that authority.

#### 1.3.10 Implementation required for option (c)

The exposure monitor and enforcement path do not exist yet and are mandatory before option (c) can satisfy this ADR.

The required implementation surface is bounded to:

1. **RISEx exposure read model**
   - read `/v1/portfolio/details` for the relevant account;
   - parse `summary.total_account_value` as the authoritative option-(c) `exposure_at_risk`;
   - retain `summary.total_notional` and position-level size/mark/liquidation data as secondary leverage, liquidation and reconciliation inputs;
   - never add `total_notional`, position notional or local order reservations to `summary.total_account_value`;
   - fail closed on missing, malformed, non-finite or otherwise indeterminate `summary.total_account_value`.

2. **Periodic monitor**
   - 240-second cadence for active RISEx mainnet accounts;
   - provider-read timeout capped at 60 seconds;
   - latest successful sample timestamp;
   - per-user and aggregate calculation from `summary.total_account_value`;
   - stale-sample detection at 300 seconds.

3. **Point-of-use enforcement and execution-accounting reservation**
   - synchronous fresh `summary.total_account_value` check before every exposure-increasing provider POST;
   - per-user and aggregate option-(c) ceilings evaluated only against account-value exposure;
   - PostgreSQL transaction-scoped advisory-lock serialization shared by all workers for pending execution/risk accounting;
   - durable per-Execution reservation committed before POST;
   - reservations excluded from the option-(c) account-value sum and used only for deterministic order/risk accounting and ambiguity safety;
   - ambiguous outcomes retain reservations until 4C resolves provider truth;
   - terminal release/replacement occurs under the same serialization lock that refreshes/persists relevant provider account/position truth;
   - risk-reducing actions treated separately.

4. **Breach handling**
   - account-level block;
   - global RISEx exposure-increase block on aggregate breach;
   - durable incident/audit record;
   - operator-visible alert;
   - explicit recovery/re-enable conditions.

5. **Tests**
   - under-limit pass;
   - per-user breach;
   - aggregate breach;
   - cross-margin account proves no collateral/notional double counting;
   - `total_notional` changes do not alter `exposure_at_risk` when `total_account_value` is unchanged;
   - out-of-band deposit detection;
   - stale provider data;
   - missing/malformed `summary.total_account_value`;
   - provider-read timeout;
   - reservation-before-POST ordering;
   - concurrent multi-worker reservation serialization;
   - ambiguous outcome and crash retain reservation;
   - definitive resolution releases/replaces reservation without an accounting gap;
   - reduction allowed while increase blocked;
   - restart/recovery behavior;
   - no automatic Hyperliquid fallback.

No new standalone Railway service is inherently required: the current execution-worker architecture and existing durable incident/system-control primitives can host the monitor if the implementation preserves task isolation and point-of-use checks.

This is a bounded medium-size implementation. The provider read model is now concretely anchored to `summary.total_account_value`; leverage/notional controls remain independent deterministic controls rather than components of the option-(c) loss-budget formula.

### 1.4 Operational enforcement and DISARM timing

The five-minute window defined above is the maximum accepted healthy-telemetry detection gap for out-of-band exposure changes. It is not automatically a guarantee about the current DISARM control path.

Current implementation evidence shows:

- the consumer task polls the RISEx control channel before taking the next queue item, so that poll does not run again while the same consumer is inside a long `handle_job_id()`;
- a separate maintenance task also polls the RISEx control channel and therefore can process DISARM while the consumer is executing a job;
- maintenance reconciliation is deadline-bounded, but the current control path has no dedicated independently scheduled fast-poll loop and no proven end-to-end upper bound from persisted DISARM request to effective window disarm.

Therefore the current implementation must **not** claim that DISARM is guaranteed to take effect within five minutes, within five seconds, or specifically only after the active job completes.

Before ADR-0006 can become `Accepted`, the RISEx mainnet kill-switch path must provide a measured and regression-tested maximum effect time of no more than **5 minutes** from successful persistence of a valid SUPERADMIN DISARM request to the point at which every subsequent RISEx signed provider POST is blocked.

The preferred implementation is a dedicated control-poll task independent of:

- queue-job duration;
- reconciliation duration;
- exposure-monitor execution;
- normal maintenance work.

DISARM must continue to serialize with the existing submission boundary so that a request already irreversibly transmitted is treated as potentially executed, while any later POST is blocked.

Until this bound is implemented and demonstrated behaviorally, the kill-switch timing gate remains **BLOCKED**.

### 1.5 User disclosure and explicit acknowledgement

The option-(c) risk must be presented **inside the provider-selection / activation flow before the user's first RISEx mainnet activation**.

A repository document, terms page that the user is not required to view, settings metadata, or a stored `risk_disclosure_version` value by itself does not constitute disclosure or acknowledgement.

The minimum disclosure shown in the activation flow must state clearly that:

- the RISEx signer currently observed by TRAXION is granted `MoveFund` authority in addition to the Perps capability TRAXION needs;
- TRAXION cannot currently verify the complete source code of the relevant RISEx Authorization/Router implementations;
- RISEx has previously changed security-relevant contract implementations without a provider upgrade notice identified by TRAXION;
- under option (c), an out-of-band increase in exposure may remain undetected for up to **5 minutes** under healthy provider telemetry;
- under option (c), the capital economically exposed to signer compromise is measured by RISEx `summary.total_account_value`; gross position notional is monitored separately as a leverage/risk signal and is not added to that account-value measure;
- the observed per-user confinement ceiling is **25,000 USDC**, but this is an operational/detective ceiling and not a cryptographic guarantee because the user can independently deposit additional collateral;
- if the ceiling is exceeded or exposure state becomes stale/unknown, TRAXION blocks additional exposure but does not automatically withdraw or move the user's funds;
- this unresolved `MoveFund` authorization risk is specific to the RISEx execution path and is not a property of TRAXION's current Hyperliquid execution path.

The user must make an explicit affirmative acknowledgement before RISEx mainnet can be activated. Passive display, continued use, pre-checked consent, or merely storing a disclosure version is insufficient.

PR C must persist the acknowledgement in an additive durable record, proposed as:

`provider_risk_acknowledgements`

with at least:

- `user_id`;
- `execution_provider = 'risex'`;
- `execution_network = 'mainnet'`;
- `disclosure_version`;
- a cryptographic hash of the exact disclosure text shown;
- `acknowledged_at`;
- the execution epoch or activation request to which the acknowledgement applied.

A uniqueness rule must prevent one acknowledgement row from being silently rewritten into acceptance of a later disclosure version.

A material change to any of the following requires a new disclosure version and a new affirmative acknowledgement before the next RISEx mainnet activation:

- signer permission model;
- source/provenance status;
- exposure ceiling;
- detection window;
- definition of capital at risk;
- provider upgrade model;
- accepted §1 risk treatment.

If the decision owner elects **not** to disclose this risk to users, that choice must be made as an explicit amendment to this ADR before mainnet enablement. The amendment must:

- state that user-facing disclosure is intentionally omitted;
- identify the decision owner;
- record the rationale for accepting undisclosed user exposure;
- remove the disclosure/acknowledgement gate from the acceptance table explicitly.

Silence, missing UI work, schedule pressure or absence of an acknowledgement record must never be interpreted as a decision not to disclose.

Under the current `Proposed` ADR, the decision is: **disclosure and explicit recorded acknowledgement are mandatory**.

### 1.6 Transitional nature of option (c)

Option (c) is temporary.

It exists solely to permit a tightly bounded initial production phase while RISEx-dependent least-privilege information or infrastructure is unavailable.

It must not become the permanent production authorization model by inertia.

Mandatory review date:

**2026-10-20**

The review must occur earlier if RISEx provides:

- authoritative `enablePermission` / `disablePermission` typed-data documentation;
- a supported Perps-only signer registration flow;
- a production-ready `BaseSubAccount` or equivalent cryptographic isolation mechanism;
- new information showing that `MoveFund` is usable through the trading signer;
- new Authorization/Router behavior affecting the assumed risk boundary.

If option (a) or option (b) becomes available and passes independent verification, TRAXION must migrate away from collateral confinement as the primary `MoveFund` mitigation.

### 1.7 Decision summary for this security dimension

| Condition | Mainnet treatment |
| --- | --- |
| (a) Verified Perps-only / `MoveFund=false` signer | Preferred; may satisfy this gate |
| (b) Verified cryptographic isolation boundary | Acceptable long-term alternative |
| (c) Observed exposure ≤ 25,000 USDC/user and ≤ 75,000 USDC aggregate, with monitor/enforcement active | Temporarily acceptable with explicit residual risk |
| Exposure cannot be measured reliably | BLOCKED |
| Exposure sample older than 300 seconds | Exposure-increasing RISEx writes BLOCKED |
| User exceeds per-user ceiling | Exposure-increasing execution for that user BLOCKED |
| Aggregate observed exposure exceeds 75,000 USDC | Option (c) invalid; RISEx exposure-increasing mainnet execution BLOCKED |
| User has not acknowledged the current RISEx mainnet risk disclosure | RISEx mainnet activation BLOCKED |
| DISARM maximum effect time is not behaviorally proven ≤ 5 minutes | RISEx mainnet activation BLOCKED |
| Capital expansion beyond option-(c) ceiling requested | Requires (a), (b), or explicit ADR revision based on new evidence |

## 2. Mainnet deployment identity must be established independently

Testnet deployment identity and ADR-0005 do not establish mainnet identity.

Before acceptance, TRAXION must collect and pin the actual RISEx mainnet deployment, including at minimum:

- chain ID;
- API base URL;
- RPC identity;
- EIP-712 domain name/version;
- Authorization proxy;
- Authorization implementation;
- Router proxy;
- Router implementation;
- relevant Orders/Perps contracts;
- `VERIFY_WITNESS_TYPEHASH`;
- replay selectors;
- signer/session selectors;
- runtime bytecode hashes;
- one canonical deployment fingerprint.

The fingerprint is manually reviewed and manually pinned.

No automatic re-pin is permitted.

Any later change in a pinned security-relevant implementation invalidates the active RISEx mainnet authorization until re-reviewed.

## 3. `complete ABI/source provenance = UNKNOWN` requires explicit treatment

Current RISEx evidence records that verified source/ABI provenance for important Authorization/Router implementations is unavailable.

Selector compatibility and runtime fingerprinting prove compatibility only for the surfaces tested. They do not prove absence of additional functions, hidden authorization paths, upgrade behavior or fund-movement capabilities.

Before mainnet acceptance, TRAXION must request from RISEx authoritative information covering at least:

- complete ABI for the production Authorization and Router deployments;
- source verification or reproducible source/build information where available;
- proxy and implementation addresses;
- proxy-admin / upgrade authority model;
- EIP-712 typed-data schemas used by production;
- signer permission semantics, particularly `MoveFund`;
- session revocation semantics;
- permit nonce/replay semantics;
- provider error semantics for consumed/replayed permits;
- formal upgrade/change-notification mechanism or changelog.

Full public source verification is the preferred state.

If full source provenance remains unavailable, `provenance = UNKNOWN` may only be accepted as an explicit residual risk if all of the following compensating conditions hold:

- RISEx supplies authoritative ABI/interface and deployment information;
- TRAXION independently fingerprints the exact production runtime;
- one of the accepted §1 risk treatments is active;
- replay and revocation are behaviorally proven;
- deployment drift fails closed;
- any provider upgrade invalidates the authorization immediately;
- the residual provenance limitation is recorded in the final Accepted ADR.

If authoritative interface/deployment information is unavailable as well as verified source, the gate remains **BLOCKED**.

## 4. Replay rejection must be proven behaviorally

Architecture-level nonce analysis is not sufficient for mainnet.

Before acceptance, the dedicated negative replay experiment must establish provider behavior using an exact identical signed request under controlled conditions.

The evidence must establish:

- first signed order accepted;
- exact permit nonce observed consumed;
- identical second signed payload mechanically proven;
- same signature, action hash, nonce, deadline and client order ID;
- valid signer/session and sufficient deadline remaining;
- unchanged deployment;
- second payload actually submitted to RISEx;
- second submission rejected;
- no second order, fill or transaction side effect created.

For the ADR-0006 mainnet gate, `REPLAY_REJECTED_UNSPECIFIED` is **insufficient** to mark behavioral replay protection as proven.

To pass, the captured provider/on-chain evidence must support an explicit reviewed conclusion that the rejection was caused by replay/consumed-permit protection.

Until such attribution exists:

`behavioral_replay_rejection_proven = false`

and mainnet remains blocked.

No fabricated provider error code, guessed message string or synthetic fixture may satisfy this condition.

## 5. Revocation must be proven behaviorally

Session revocation must be tested as a real provider behavior, not only inferred from contract architecture.

The required negative test is:

1. create/verify a valid dedicated signer/session;
2. construct a request that would otherwise be accepted;
3. revoke/disable the signer through the supported RISEx mechanism;
4. confirm revocation on-chain/read-only;
5. attempt the otherwise-valid signed trading action while all unrelated prerequisites remain valid;
6. observe explicit rejection;
7. verify read-only that no order, fill, transaction or position change resulted.

The test must prove that revocation takes effect at the execution boundary and cannot be bypassed by a previously established TRAXION Operational Execution Window.

A revoked signer accepted by the provider is an immediate security failure and blocks mainnet.

## 6. 4B-bis must be complete

The production worker must be capable of transforming an immutable RISEx-bound CopyJob/intention into the complete prepared signed submission without bypassing the normal TRAXION execution controls.

The job must remain immutably bound to:

- provider;
- network;
- user/account;
- execution epoch;
- credential version;
- market identity;
- strategy/risk intent;
- client/order identity.

There must be no silent fallback from RISEx to Hyperliquid.

Preparation failure must fail closed.

Ambiguous execution must not result in blind resubmission.

## 7. 4C reconciliation is a hard mainnet prerequisite

RISEx mainnet must not be enabled with write-only execution.

TRAXION must reconcile provider truth for at least:

- submitted orders;
- accepted/rejected status;
- partial fills;
- complete fills;
- cancellations where applicable;
- position size;
- side;
- entry/relevant execution price;
- provider order identifiers;
- transaction identifiers when available;
- local ledger state;
- unresolved `SUBMITTING` / `UNKNOWN` states.

An API/read failure must never be interpreted as zero position or zero exposure.

An ambiguous POST outcome must be resolved against provider state before any resubmission can occur.

The recovery invariant is:

> prefer temporary missed execution to a duplicated order.

For covered failure scenarios, TRAXION must demonstrate:

- zero blind duplicate orders;
- eventual local/provider convergence;
- resolution within at most two normal reconciliation cycles where the provider exposes sufficient state;
- otherwise transition to an explicit unresolved/incident state that blocks further exposure for the affected account.

4C is not complete until restart, timeout, lost response and worker-crash scenarios have been exercised.

## 8. Mainnet rollout is deliberately limited

Passing the security gate does not authorize immediate unrestricted rollout.

The initial mainnet rollout uses staged exposure limits.

### Phase 0 — mainnet shadow

Before the first RISEx mainnet write:

- at least 7 consecutive days;
- real production master events;
- real production RISEx read-only data where available;
- zero RISEx provider writes;
- comparison between intended execution and provider/account constraints;
- no unresolved reconciliation/design defect.

### Phase 1 — operator canary

Initial writes are restricted to:

- maximum users: `1`;
- operator-controlled/dedicated account only;
- maximum aggregate RISEx gross notional: `500 USDC`;
- maximum observed §1 exposure-at-risk: `500 USDC`;
- maximum leverage: `2x`;
- duration: minimum `24 hours`;
- minimum completed/reconciled execution intents: `20`;
- zero unresolved ambiguous executions;
- zero security-gate violations.

### Phase 2 — limited invited rollout

Only after Phase 1 passes:

- maximum active RISEx users: `3`;
- maximum observed §1 exposure-at-risk per user: `25,000 USDC`;
- maximum aggregate observed §1 exposure-at-risk: `75,000 USDC`;
- maximum leverage: `2x`;
- duration: minimum `7 consecutive days`;
- minimum completed/reconciled executions: `100`;
- no unresolved execution older than two expected reconciliation cycles;
- no replay, signer, deployment, reconciliation or duplicate-order incident.

These are mainnet safety ceilings, not commercial limits.

Raising them requires an explicit reviewed operational change. They must not automatically scale because more users select RISEx.

## 9. RISEx-specific kill switches are mandatory

Mainnet enablement requires two independently testable stop modes.

### Exposure pause

A RISEx exposure pause must block:

- new positions;
- position increases;
- new risk-taking orders.

Risk-reducing operations may remain available only through the explicitly defined reduction/recovery path.

### Hard stop

A RISEx hard stop must prevent every new RISEx signed provider POST, including normal execution-worker activity.

Hard stop must:

- invalidate/disarm the current RISEx Operational Execution Window;
- prevent automatic re-arming;
- survive worker restart;
- default to disabled execution after process/deployment restart until explicitly re-authorized.

Authority to activate/deactivate mainnet RISEx belongs only to authenticated `SUPERADMIN` operational control.

Automatic fail-closed hard stop is also required for security-critical events including:

- deployment fingerprint drift;
- account/signer identity mismatch;
- revoked/expired signer;
- replay security failure;
- reconciliation integrity failure capable of causing duplicate or incorrect exposure;
- inability to establish the configured mainnet network/deployment identity.

### Kill-switch effectiveness criterion

The kill switch must be tested before mainnet acceptance.

From successful persistence of a valid operator hard-stop request to prevention of further RISEx provider writes:

- target: before the next attempted provider POST;
- maximum accepted and behaviorally proven effect time: `5 minutes`;
- the control path must be independent of queue-job duration and ordinary maintenance/reconciliation work.

The current implementation has not yet proven this bound, so this gate remains BLOCKED until the dedicated control path or equivalent behavior is implemented and tested.

A POST already irreversibly transmitted before the stop became effective is treated as potentially executed and must be reconciled. The kill switch must never assume that an in-flight request was cancelled.

## 10. Rollback and incident behavior

If a defect appears after RISEx mainnet activation:

1. hard-stop RISEx new writes immediately;
2. do not automatically switch affected users to Hyperliquid;
3. do not automatically close positions;
4. preserve jobs, executions, provider identifiers and diagnostic evidence;
5. reconcile every `SUBMITTING`, `UNKNOWN`, partial fill and open position against RISEx;
6. freeze provider switching for accounts with non-flat or unresolved state;
7. if the defect is code-related, redeploy the last known stable commit;
8. additive DB migrations remain in place during code rollback unless there is a separately reviewed reason to downgrade;
9. any DB downgrade/restore follows the existing backup/PITR runbook;
10. any compensating close, signer revocation or collateral action is a separately authorized incident operation;
11. if the incident changes a security assumption in this ADR, RISEx mainnet returns to `BLOCKED` and requires a new acceptance review before reactivation.

Rollback must preserve the principle that provider state is authoritative for actual execution state.

No rollback may manufacture local state to make the ledger appear consistent.

## 11. Provider switching remains fail-closed

A RISEx incident or outage must never cause automatic fallback to Hyperliquid.

Switching execution provider requires the existing provider-switch invariants:

- strategy paused;
- old destination verified flat;
- no open/conditional orders;
- no pending/retrying/processing execution;
- no unresolved execution result;
- reconciliation complete;
- new execution epoch created and bound explicitly.

Provider switching never migrates collateral or positions.

## 12. Evidence package required for `Accepted`

The Pull Request that changes this ADR from `Proposed` to `Accepted` must contain or link to reviewable evidence for every gate.

At minimum the review must be able to verify:

| Gate | Required result |
| --- | --- |
| Mainnet deployment identity | PASS and pinned |
| Mainnet Authorization/Router fingerprint | reviewed and immutable until next review |
| ADR-0002 mainnet re-evaluation | PASS under §1 |
| `MoveFund` treatment | (a), (b), or constrained transitional (c) active |
| Option-(c) exposure monitor, if used | PASS |
| Option-(c) pre-POST exposure enforcement, if used | PASS |
| Option-(c) in-flow disclosure + explicit persisted acknowledgement, if used | PASS |
| Provenance | resolved or explicitly accepted under §3 compensating controls |
| Replay | behavioral PASS |
| Post-revoke | behavioral PASS |
| 4B-bis | complete |
| 4C reconciliation | complete |
| Ambiguity resolution | proven fail-closed |
| Duplicate-order controls | PASS |
| Kill switch exposure pause | PASS |
| Kill switch hard stop | PASS within required latency |
| Restart/disarm behavior | PASS |
| Rollback drill | PASS |
| Mainnet shadow period | completed |
| CI / security audit | all required checks green |
| Open P0/P1 RISEx security findings | zero |

A textual assertion such as “tested manually” is insufficient unless accompanied by reproducible evidence or sanitized runtime records.

## 13. Transition from Proposed to Accepted

This ADR remains `Proposed` throughout:

- 4B-bis implementation;
- 4C implementation;
- option-(c) monitor/enforcement implementation if needed;
- replay live testing;
- post-revoke testing;
- PR C implementation;
- mainnet deployment investigation;
- security audit remediation.

It transitions to `Accepted` only in the PR whose explicit purpose is to enable RISEx mainnet and only if every mandatory gate above is already satisfied.

The acceptance PR must identify the exact:

- Git commit;
- Railway project/environment/services;
- RISEx mainnet chain/deployment fingerprint;
- configuration values controlling enablement;
- rollback commit;
- active §1 risk treatment;
- initial rollout phase and exposure ceilings.

Merging earlier implementation PRs must not implicitly change this ADR to Accepted.

## 14. Conditions that immediately invalidate Accepted status

After mainnet activation, RISEx execution must return to BLOCKED if any of the following occurs:

- security-relevant RISEx deployment fingerprint changes;
- Authorization or Router implementation changes;
- signer permissions expand unexpectedly;
- `MoveFund` becomes available outside the accepted §1 treatment;
- a replayed signed request is accepted;
- a revoked signer successfully executes;
- unresolved duplicate-order behavior is observed;
- 4C cannot reconcile provider truth reliably;
- a provider response ambiguity causes or risks blind resubmission;
- kill switch does not prevent new writes within its verified operational bound;
- mainnet network/account/deployment identity becomes uncertain;
- provenance/interface information supplied by RISEx is contradicted by runtime behavior;
- a new fund-movement path invalidates ADR-0002/ADR-0003 assumptions;
- option (c), when active, exceeds its aggregate observed ceiling;
- option (c), when active, cannot establish sufficiently fresh exposure state.

Re-enable requires explicit review. No automatic recovery may reopen RISEx mainnet after a security invalidation.

## 15. What this gate does NOT prove

Even after ADR-0006 becomes Accepted, TRAXION will not claim that it has proven:

- complete semantic correctness of RISEx contracts for code whose source remains unavailable;
- absence of undisclosed or future provider contract functionality;
- absence of provider/admin upgrade risk;
- absence of future RISEx regressions;
- economic solvency or availability of RISEx;
- guaranteed fill quality, liquidity or slippage;
- protection against compromise of the user's own wallet/device;
- correctness or profitability of the upstream trading strategy;
- absence of all software vulnerabilities in TRAXION or RISEx;
- safety of arbitrary leverage or arbitrary capital exposure;
- authorization for withdrawals or collateral movement by TRAXION;
- safety of automatic provider fallback;
- a cryptographic guarantee that option-(c) exposure cannot temporarily exceed its observed ceilings;
- safety beyond the rollout limits explicitly approved under this ADR.

Acceptance means only that the specific RISEx mainnet execution path reviewed by TRAXION has satisfied the defined technical, behavioral and operational gate at the stated deployment and risk limits.

It is not a blanket assertion that RISEx or TRAXION is risk-free.

## Consequences

### Positive

- The production decision is made against criteria written before implementation is complete.
- Testnet assumptions cannot silently become mainnet assumptions.
- The unresolved `MoveFund` risk is separated into cryptographic and economic treatments rather than conflated.
- Behavioral evidence is required for replay and revocation.
- The provider cannot be enabled without reconciliation and ambiguity handling.
- Real-capital exposure starts with explicit numerical ceilings.
- The detection gap of economic confinement is quantified instead of hidden.
- Users must explicitly acknowledge the RISEx-specific residual risk before activation.
- Operational stop and rollback behavior are part of the authorization decision rather than an afterthought.

### Negative / residual

- The preferred `MoveFund` solution may remain blocked until RISEx exposes a least-privilege mechanism or an enforceable isolation model.
- Option (c) does not prevent out-of-band user deposits and therefore cannot guarantee its observed ceilings continuously.
- During healthy telemetry an external exposure increase may remain undetected for up to 5 minutes under the defined cadence/timeout.
- During provider-read outage the duration of unknown external exposure cannot be bounded, although TRAXION exposure-increasing writes fail closed once evidence becomes stale.
- The replay gate may remain blocked if RISEx rejects replay but does not provide evidence sufficient to attribute the rejection to permit replay protection.
- Source provenance may remain a residual external dependency on RISEx.
- The initial mainnet rollout is deliberately slower and capital-constrained.

These residuals are intentional and must remain visible in the final mainnet decision.

## Related documents

- `docs/adr/ADR-0002-risex-session-key-authorization-model.md`
- `docs/adr/ADR-0003-risex-fund-movement-negative-probe-applicability.md`
- `docs/adr/ADR-0004-risex-continuous-execution-authorization.md`
- `docs/adr/ADR-0005-risex-testnet-deployment-repin.md`
- `docs/security/risex-testnet-runtime-evidence-2026-09-10.md`
- `docs/superpowers/specs/2026-09-09-risex-follower-integration-design.md`
- `docs/superpowers/specs/2026-09-19-risex-negative-replay-probe-design.md`
- `SPEC.md`
- `RUNBOOK.md`
