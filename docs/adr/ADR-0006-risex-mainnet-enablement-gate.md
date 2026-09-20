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

For each RISEx user account:

```text
exposure_at_risk =
    deposited_collateral
    + liquidatable_value_of_open_positions
```

The relevant amount is therefore the capital actually exposed inside RISEx, not the user's external wallet balance, total wealth or total portfolio value.

Where RISEx exposes an authoritative liquidation/close value for open positions, TRAXION must use that provider value.

If RISEx does not expose a sufficiently reliable liquidation-value field, TRAXION must use a conservative substitute:

```text
sum(abs(position_size) × current_mark_price)
```

for open positions.

If neither value can be established reliably, `exposure_at_risk` is considered **UNKNOWN** and the account fails closed for new exposure.

Missing data must never be interpreted as zero.

#### 1.3.2 Accepted loss budget and ceilings

Option (c) is based on the amount of real capital TRAXION is prepared to have economically exposed under the unresolved signer-authority model, not on an estimate of what users are expected to deposit.

The initial accepted observed exposure budget is:

```text
RISEX_USER_EXPOSURE_CEILING_USDC = 1,000
RISEX_TOTAL_EXPOSURE_CEILING_USDC = 2,500
```

The per-user ceiling limits concentration: compromise or misuse associated with one account should not intentionally place more than 1,000 USDC of observed capital-at-risk inside the transitional perimeter.

The aggregate ceiling is the project-level loss budget for option (c): TRAXION does not intentionally authorize continued exposure-increasing operation while more than 2,500 USDC of observed user capital is inside this unresolved `MoveFund` trust boundary.

These values are deliberately aligned with the initial restricted rollout rather than commercial scale.

They are policy ceilings over **observed** exposure. Because users can independently deposit capital and TRAXION is non-custodial, they are not cryptographically guaranteed maximum-loss bounds. The detection gap and provider-read outage limitation are recorded explicitly below.

The aggregate ceiling must not increase automatically because more users request RISEx access.

Any increase requires an explicit ADR amendment based on new evidence and a new loss-budget decision.

#### 1.3.3 Who verifies exposure and how often

The RISEx exposure monitor is a mandatory production component for option (c).

The `execution-worker` operational layer, or a dedicated component sharing the same authoritative database and provider-read model, must collect current RISEx exposure for every mainnet account enabled for TRAXION execution.

Required cadence:

```text
RISEX_EXPOSURE_MONITOR_INTERVAL_SECONDS = 30
RISEX_EXPOSURE_READ_TIMEOUT_SECONDS = 10
RISEX_EXPOSURE_SAMPLE_MAX_AGE_SECONDS = 60
```

Exposure must also be checked synchronously immediately before every candidate operation capable of increasing exposure.

The periodic monitor and the pre-POST check serve different purposes:

- the periodic monitor detects out-of-band changes such as a user depositing additional collateral;
- the pre-POST check prevents TRAXION from intentionally adding exposure when the latest verified state is already at or near the ceiling.

Cached account exposure older than `RISEX_EXPOSURE_SAMPLE_MAX_AGE_SECONDS` cannot authorize an exposure-increasing POST.

#### 1.3.4 Enforcement on threshold breach

If a user's verified:

`exposure_at_risk > 1,000 USDC`

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

`2,500 USDC`

option (c) is no longer within its accepted loss budget.

TRAXION must:

- block RISEx exposure-increasing writes globally;
- create a high-priority operational/security incident;
- keep risk-reducing recovery operations separately controlled;
- refuse automatic re-enable;
- require aggregate verified exposure to return to or below the ceiling, or require option (a) or (b), before normal RISEx execution resumes.

Above 2,500 USDC aggregate exposure, option (c) cannot be used as the basis for continued scale-out.

#### 1.3.5 Pre-POST exposure check

Before every exposure-increasing RISEx provider POST, TRAXION must establish from sufficiently fresh provider truth:

```text
current_user_exposure_at_risk
+ conservative_max_incremental_exposure(candidate_order)
<= 1,000 USDC
```

and:

```text
current_aggregate_exposure_at_risk
+ conservative_max_incremental_exposure(candidate_order)
<= 2,500 USDC
```

The candidate-order calculation must use the conservative worst-case economic effect of the order.

If either calculation cannot be completed:

`POST = BLOCKED`.

A post-trade monitor does not replace this point-of-use check.

#### 1.3.6 TRAXION does not control out-of-band deposits

TRAXION can enforce whether TRAXION itself submits another order.

TRAXION cannot, under the current non-custodial architecture, prevent an account owner from independently depositing additional collateral into RISEx.

Therefore option (c) is **detective plus reactive**, not a preventive custody boundary.

If the user independently deposits above the configured limit, the system has not prevented the exposure increase. It can only detect it on the next successful provider observation and then block further TRAXION exposure-increasing activity.

The ceiling must never be described as a guaranteed cap on the amount physically present in the RISEx account.

#### 1.3.7 Quantified detection gap

Under healthy provider telemetry, an out-of-band collateral increase can occur immediately after a successful sample.

With:

```text
monitor interval = 30 seconds
provider read timeout = 10 seconds
```

the accepted maximum normal detection window is:

```text
40 seconds
```

from an out-of-band change occurring immediately after a successful observation to completion of the next scheduled observation, assuming the provider read succeeds within the configured timeout.

For up to that 40-second window, actual exposure can exceed the declared ceiling without TRAXION knowing it.

That detection gap is explicitly accepted as part of temporary option (c).

If RISEx account-state reads fail, the actual amount of an out-of-band deposit cannot be bounded by TRAXION until provider visibility returns.

After the last successful sample becomes older than:

`60 seconds`

TRAXION must fail closed for every exposure-increasing RISEx write and raise an incident/alert.

This stale-data hard stop prevents TRAXION from adding further exposure, but it cannot retroactively prevent or measure external deposits made during the provider-read outage.

Accordingly, during a provider-read outage the duration of **unknown external exposure is not cryptographically bounded**. This limitation is an explicit residual risk of option (c) and is another reason option (c) is transitional rather than equivalent to (a) or (b).

#### 1.3.8 Residual-risk statement

Under option (c), TRAXION explicitly accepts the following residual-risk model:

> If an attacker obtains effective control of a RISEx signer that still possesses `MoveFund` authority, the capital potentially reachable by that attacker is the collateral deposited in the affected RISEx account plus the liquidatable value of its open positions, subject to the actual capabilities enforced by RISEx.

For multiple compromised accounts, the potential aggregate exposure is the sum of those account-level values.

Under normal monitored operation TRAXION intends to keep observed exposure within 1,000 USDC per user and 2,500 USDC aggregate.

Those figures are the accepted transitional loss budget, not a cryptographic guarantee that loss cannot exceed them.

An out-of-band user deposit can temporarily exceed the ceiling before detection, and a provider-read outage can make that excess unobservable until reads recover.

Collateral confinement limits accepted economic exposure operationally.

It does not prove that the signer lacks fund-movement authority.

It does not prevent misuse of that authority.

#### 1.3.9 User disclosure and acknowledgement

The RISEx-specific residual `MoveFund` risk must be disclosed to users before they enable RISEx mainnet execution.

It must not be hidden in internal documentation only.

PR C must require a clear acknowledgement before first RISEx mainnet activation. The disclosure must state, in substance:

- the current RISEx signer model may expose broader authority than the Perps execution capability TRAXION requires;
- TRAXION's temporary protection is an economic collateral-confinement policy, not cryptographic least privilege;
- capital deposited in the RISEx account, plus the liquidatable value of open positions, may be exposed if the signer is compromised or the provider authorization behaves more broadly than expected;
- TRAXION cannot prevent the user from independently depositing above the configured ceiling;
- when excess exposure is detected, TRAXION blocks additional exposure but does not automatically withdraw user funds;
- this specific unresolved RISEx `MoveFund` authorization risk is not part of the current Hyperliquid execution path.

The UI must show the currently applicable per-user ceiling.

The acknowledgement version and timestamp must be persisted so that a material change in this risk model can require a new acknowledgement.

A user who does not acknowledge the disclosure cannot enable RISEx mainnet execution.

#### 1.3.10 Implementation required for option (c)

The exposure monitor and enforcement path do not exist yet and are mandatory before option (c) can satisfy this ADR.

The required implementation surface is bounded to:

1. **RISEx exposure read model**
   - current collateral/balance read;
   - open-position read;
   - provider mark/liquidation-value input;
   - deterministic `exposure_at_risk` calculation;
   - fail-closed handling of missing/malformed provider state.

2. **Periodic monitor**
   - 30-second cadence for active RISEx mainnet accounts;
   - latest successful sample timestamp;
   - per-user and aggregate calculation;
   - stale-sample detection at 60 seconds.

3. **Point-of-use enforcement**
   - synchronous fresh exposure check before every exposure-increasing provider POST;
   - conservative candidate-order increment;
   - per-user and aggregate ceiling enforcement;
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
   - out-of-band deposit detection;
   - stale provider data;
   - malformed/missing balance or position data;
   - provider-read timeout;
   - pre-POST race protection;
   - reduction allowed while increase blocked;
   - restart/recovery behavior;
   - no automatic Hyperliquid fallback.

No new standalone Railway service is inherently required: the current execution-worker maintenance/control architecture and existing durable incident/system-control primitives can host the monitor if the implementation preserves isolation and point-of-use checks.

This is a bounded medium-size implementation: one provider read/evaluation path, one periodic monitor, one point-of-use enforcement boundary, one incident/alert path and a focused regression suite. It is not expected to be the dominant schedule risk relative to unresolved provider-side RISEx information, but it remains a hard mainnet prerequisite.

### 1.4 Transitional nature of option (c)

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

### 1.5 Decision summary for this security dimension

| Condition | Mainnet treatment |
| --- | --- |
| (a) Verified Perps-only / `MoveFund=false` signer | Preferred; may satisfy this gate |
| (b) Verified cryptographic isolation boundary | Acceptable long-term alternative |
| (c) Observed exposure ≤ 1,000 USDC/user and ≤ 2,500 USDC aggregate, with monitor/enforcement active | Temporarily acceptable with explicit residual risk |
| Exposure cannot be measured reliably | BLOCKED |
| Exposure sample older than 60 seconds | Exposure-increasing RISEx writes BLOCKED |
| User exceeds per-user ceiling | Exposure-increasing execution for that user BLOCKED |
| Aggregate observed exposure exceeds 2,500 USDC | Option (c) invalid; RISEx exposure-increasing mainnet execution BLOCKED |
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
- maximum observed §1 exposure-at-risk per user: `1,000 USDC`;
- maximum aggregate observed §1 exposure-at-risk: `2,500 USDC`;
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

From successful operator hard-stop request to prevention of further RISEx provider writes:

- target: before the next attempted provider POST;
- maximum accepted control-plane latency: `5 seconds`.

A POST already irreversibly transmitted before the stop was accepted is treated as potentially executed and must be reconciled. The kill switch must never assume that an in-flight request was cancelled.

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
| Option-(c) disclosure/acknowledgement, if used | PASS |
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
- During healthy telemetry an external exposure increase may remain undetected for up to 40 seconds under the defined cadence/timeout.
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
