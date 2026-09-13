# ADR-0003 — RISEx fund-movement negative-probe applicability

- **Status:** Proposed
- **Date:** 2026-09-13
- **Scope:** TRAXION / RISEx pre-order security gate and fund-movement negative evidence
- **Decision owner:** TRAXION project owner
- **Related decision:** ADR-0002 — RISEx session-key authorization model
- **Mandatory review date:** 2026-12-13

## Context

`authorize_pre_order_probe()` currently requires both of the following behavioral evidence fields to be exactly `True` before it can issue an attested pre-order gate:

- `fund_movement_rejected`;
- `withdrawal_rejected`.

Their intended semantics are behavioral: a concrete operation was attempted through the relevant signer authorization path and RISEx rejected it.

The current runtime evidence path does not produce either fact. `collect_public_signer_evidence()` deliberately returns:

- `fund_movement_rejected=None`;
- `withdrawal_rejected=None`.

The `True` values that exist in the repository for these fields are test fixtures. They are not provider observations and must not be promoted into runtime evidence.

PASSO 2E recorded this mismatch before its RED implementation commit and deliberately refused to manufacture those facts. It requires an already-attested pre-order gate and does not convert the negative-probe fields into operator assertions.

ADR-0002 separately established the current architectural finding that the reviewed RISEx session-key path has no documented fund-movement primitive that consumes the session-key authorization used for trading. That finding is the premise this ADR evaluates against the two behavioral negative-probe requirements.

## Reviewed fund-movement surfaces

The current RISEx documentation and interface material reviewed for the pinned integration model support the following conclusions:

1. `withdraw(address,Currency,uint256)` operates on `msg.sender`. Possession of the registered session-key credential does not make the session signer the account caller for that operation.
2. `permitTransferFrom` requires an EIP-712 signature from the owner. It does not use the trading session-key signature as the owner authorization.
3. No `isValidSignature` or EIP-1271 authorization path was identified on the reviewed `CollateralManager` surface that would allow the account to delegate these fund-movement calls to the session key through contract-signature validation.
4. No reviewed fund-movement operation consumes the trading-path `VerifyWitness` used by the documented session-key flow.

The result is an architectural absence of an attemptable session-key fund-movement target in the currently reviewed model.

That is materially different from a behavioral rejection. A behavioral statement such as `fund_movement_rejected=True` or `withdrawal_rejected=True` can only be made after a real, relevant operation exists and an attempted invocation is actually rejected. When no such operation exists, there is nothing legitimate to invoke for the negative probe.

## Problem

The current unconditional requirements encode the proposition:

> a session-key fund-movement or withdrawal operation exists, it was attempted, and the provider rejected it.

The reviewed architecture instead supports:

> no session-key-authorized fund-movement or withdrawal operation has been identified to attempt.

Treating the second proposition as evidence for the first would fabricate a behavioral observation. Keeping both negative-probe booleans unconditionally mandatory also makes the gate impossible to satisfy honestly while the documented architecture has no probe target.

The requirements are therefore **inapplicable in the current architecture**, not failed.

This is the same class of decision already addressed by ADR-0002 for `perps_only_scope`: a security criterion can be sensible in principle while still being unsatisfiable or inapplicable to the actual provider authorization model.

## Decision

### 1. Make the behavioral negative probes conditional, rather than deleting the security concept

`fund_movement_rejected` and `withdrawal_rejected` are retained as security evidence concepts, but they must no longer be unconditional prerequisites when the reviewed architecture proves that no session-key-authorized fund-movement path exists.

The preferred policy is **conditional applicability** rather than permanent removal.

This is chosen because it preserves the original defensive intent if RISEx later exposes a session-key-signable fund-movement primitive, while avoiding fabricated evidence in the present architecture. Permanent removal would discard a useful future regression requirement and would require rediscovering the same control if the provider authorization surface expands.

### 2. Use `fund_movement_path_absent` as the current fail-closed prerequisite

For the currently reviewed RISEx model, the relevant prerequisite is the existing ADR-0002 evidence:

`fund_movement_path_absent is True`

This is an explicit reviewed architectural assertion. It defaults fail-closed and is not inferred from missing data.

While that assertion remains valid:

- `fund_movement_rejected` is **not applicable**;
- `withdrawal_rejected` is **not applicable**;
- neither field may be synthesized, defaulted or asserted as `True` merely to satisfy the gate;
- their honest runtime value may remain `None` because no behavioral probe occurred.

`None` in this state means “not applicable / no attemptable path,” not “provider rejection observed.”

If `fund_movement_path_absent` is `False`, missing or uncertain, this ADR provides no unlock. The system remains fail-closed.

### 3. Do not treat negative-probe evidence as a substitute for an invalidated architectural assumption

If a session-key-authorized fund-movement path is discovered, `fund_movement_path_absent=True` immediately ceases to be valid. The RISEx write gate must return to BLOCKED under ADR-0002's invalidation rule pending security re-review.

At that point both behavioral negative-probe requirements become necessary again before any future unlock decision:

- a real fund-movement attempt through the newly identified session-key authorization path must be observed and rejected;
- a real withdrawal attempt through the relevant session-key authorization path must be observed and rejected.

Passing those negative probes is necessary evidence, not by itself sufficient authority to re-enable writes. The changed provider authorization model must still be reviewed under the applicable ADR process.

### 4. No fabricated evidence

TRAXION must never convert architectural absence into behavioral success.

Specifically:

- `fund_movement_rejected=True` may only represent an observed rejection of an applicable fund-movement probe;
- `withdrawal_rejected=True` may only represent an observed rejection of an applicable withdrawal probe;
- test fixtures, operator assertions, documentation inference or the absence of an endpoint must not be used to manufacture either `True` value.

This preserves the distinction between reviewed architectural evidence and observed provider behavior.

## Applicability model

The security interpretation is therefore:

| State | `fund_movement_path_absent` | Behavioral negative probes | Result for this security dimension |
| --- | --- | --- | --- |
| Reviewed current architecture: no session-key fund-movement path | `True` | Not applicable; do not fabricate | May proceed to the remaining independent gates |
| Path absence unknown or unreviewed | not explicitly `True` | Cannot repair missing architectural evidence | BLOCKED |
| Session-key-signable fund-movement primitive discovered | current `True` assertion invalidated | Both rejection proofs become mandatory before any future unlock review | BLOCKED pending behavioral proof and re-review |

This ADR does not authorize mainnet execution and does not weaken any independent readiness, deployment, replay, account/signer binding, environment-isolation or pre-POST freshness control.

## Operational invalidation conditions

The current not-applicable status of the two behavioral negative probes immediately expires if RISEx publishes, deploys or documents any primitive that can move, withdraw, transfer, bridge or otherwise reassign account collateral using a session-key signature or a session-key `VerifyWitness`.

The review must also be brought forward if any of the following changes could create such a path:

- a new fund-movement or withdrawal API endpoint;
- a new contract function accepting session-key authorization;
- a new EIP-712 type for fund movement;
- an EIP-1271 / `isValidSignature` integration on `CollateralManager` or another relevant account/collateral contract;
- a change to `VerifyWitness` or its consumers;
- a change to the Authorization or Router deployment identity;
- a change in session-key caller/delegation semantics;
- new public contract source or ABI material that contradicts the current architectural analysis.

When any invalidation condition occurs:

1. `fund_movement_path_absent=True` must no longer be asserted;
2. RISEx signed writes remain or become disabled;
3. `fund_movement_rejected` and `withdrawal_rejected` return to mandatory behavioral evidence for the security review;
4. the probes must target real documented primitives and record actual provider/contract rejection;
5. no future unlock occurs solely because the negative probes reject — the changed authorization model requires explicit re-review.

## Mandatory review date

This ADR must be reviewed no later than **2026-12-13**, the same mandatory review date as ADR-0002.

The dates are intentionally synchronized because both decisions depend on the same architectural premise: the continued absence of a session-key-authorized fund-movement path. A single coordinated review avoids a state in which ADR-0002 accepts that premise while ADR-0003 evaluates stale probe applicability, or vice versa.

The event-driven invalidation conditions above require an earlier review whenever the provider surface changes before that date.

## Consequences

### Positive

- Runtime evidence remains truthful: no rejection is claimed when no operation was attempted.
- The gate can be aligned with the provider architecture without inventing behavioral proof.
- `fund_movement_path_absent` remains explicit and fail-closed rather than becoming an implicit assumption.
- The original negative-probe defense is preserved for any future RISEx authorization expansion.
- The decision remains synchronized with ADR-0002's residual-risk review cycle.

### Negative / residual risks

- The safety conclusion remains bounded by the currently reviewed RISEx documentation, interfaces and pinned deployment model.
- A provider-side capability can invalidate the conclusion before TRAXION code changes.
- A follow-up implementation PR is required before the runtime gate reflects this ADR.
- Until that follow-up code is reviewed and merged, the existing `authorize_pre_order_probe()` behavior remains unchanged and continues to require both negative booleans to be `True`.

## Implementation boundary

This ADR PR is decision-only.

It must **not** modify:

- `authorize_pre_order_probe()`;
- `RISExSignerCapabilityEvidence`;
- `collect_public_signer_evidence()`;
- readiness or signed transport code;
- tests or fixtures;
- Railway configuration;
- write-enable flags.

After this ADR is accepted, a separate TDD implementation PR may change the gate so the two behavioral rejection requirements are conditional on the existence of an attemptable session-key fund-movement path, while preserving `fund_movement_path_absent` as the current explicit fail-closed criterion.

## Related evidence and decisions

- `docs/adr/ADR-0002-risex-session-key-authorization-model.md`
- `docs/superpowers/specs/2026-09-13-risex-signed-execution-orchestration-design.md`
- `backend/app/security/risex_pre_order_gate.py`
- `backend/app/security/risex_signer_probe.py`
