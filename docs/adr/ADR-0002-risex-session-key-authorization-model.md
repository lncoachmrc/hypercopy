# ADR-0002 — RISEx session-key authorization model

- **Status:** Accepted
- **Date:** 2026-09-13
- **Scope:** TRAXION / RISEx session-key authorization and residual-risk gate
- **Decision owner:** TRAXION project owner
- **Supersedes:** ADR-0001 for the RISEx verification environment and signer-security decision
- **Mandatory review date:** 2026-12-13

## Context

This ADR records the RISEx authorization model adopted after read-only evidence collected on 2026-09-12 and 2026-09-13.

No signature, transaction or provider-side write was required to collect the evidence summarized here.

The pinned RISEx testnet Authorization proxy is:

`0x6DA86F486b5E6536358F5b122dBe184522CA0eE3`

It was independently confirmed as `system.auth` through `GET /v1/system/config` and is part of the deployment identity already pinned by TRAXION.

The observed EIP-1967 Authorization implementation is:

`0x47253b880ec432a32485e30510c8eb6afe721cbc`

Observed runtime implementation size: `15558` bytes. This is consistent with the previously accepted deployment preflight and fingerprint evidence.

The following selectors were verified present in the pinned Authorization runtime:

- `enablePermission` → `0x77007534`
- `disablePermission` → `0x06991816`
- `sessionKeys` → `0x96ade1f9`
- `getSessionKeyStatus` → `0xdd962cb2`
- `hasPermission` → `0xed82f4b8`
- `registerSigner` → `0x8a10cb9e`

## Observed signer permission state

A session key created through the RISEx testnet dashboard returned the following read-only state:

- `sessionKeys.permissionBitmap = 4294967295`
- hexadecimal bitmap: `0xFFFFFFFF`
- `sessionKeys.status = 1`
- `hasPermission(All) = true`
- `hasPermission(Perps) = true`
- `hasPermission(Spot) = true`
- `hasPermission(MoveFund) = true`

The RISEx UI states that API wallets can sign trading actions but cannot withdraw funds. The Authorization state observed on-chain grants `MoveFund`. These two statements therefore diverge at the permission-model level.

This divergence is treated as security-relevant evidence. It does not, by itself, prove that a session key can successfully execute a fund-movement operation.

## Analysis — why `MoveFund` is not currently exploitable by the observed session-key path

The current conclusion is architectural rather than behavioral.

Based on the documented interfaces reviewed for the pinned integration model:

- `withdraw(address,Currency,uint256)` operates on `msg.sender`; the registered session signer is not the account caller merely by possessing its session-key credential;
- `permitTransferFrom` requires an EIP-712 signature from the owner, not from the session signer;
- `VerifyWitness` includes a signed `permission` field, but this permit family is used by the documented trading encoders for place order, cancel order, leverage and margin-mode operations;
- no documented fund-movement operation has been identified that consumes a session-key `VerifyWitness` as its authorization path.

Therefore, the current evidence shows **permission granted, but no documented usable fund-movement path from the session key**.

This is an architectural proof bounded by the currently observed and documented interfaces. It is not a behavioral proof that every possible provider path rejects fund movement.

## Decision

### 1. Replace the `perps_only_scope` unlock criterion

`perps_only_scope` cannot be `True` for the dashboard-created session key because the observed Authorization permission state includes `All`, `Spot` and `MoveFund` in addition to `Perps`.

It therefore cannot remain the criterion that decides whether RISEx testnet execution may progress.

The security gate is reformulated as follows:

- `perps_permission = true` remains mandatory;
- `MoveFund = true` is not independently blocking **while the architectural condition documented above remains valid**, namely that there is no documented fund-movement path accepting a session-key signature;
- the absence of such a path is a reviewed security assumption, not a permanent property of RISEx.

### 2. Accept the residual risk with bounded capital

TRAXION accepts this residual authorization risk only with a dedicated RISEx account whose balance is limited to the amount of capital the operator explicitly accepts exposing to this provider-integration risk.

The dedicated RISEx account is part of the risk control itself. Production/mainnet capital must not be treated as implicitly covered by the testnet conclusion in this ADR.

### 3. Keep permission reduction as the target state

Reducing the signer permission set through `disablePermission` remains the preferred least-privilege target.

That path is currently blocked because the required typed EIP-712 data for permission mutation is not sufficiently documented in the material reviewed by TRAXION and was not found in the publicly reviewed risechain repositories used for this integration. A clarification request has been sent to RISEx.

Until the permission-mutation signing model is authoritatively documented and independently verified, TRAXION must not invent or infer a signing payload for `disablePermission`.

### 4. Record the stronger least-privilege architecture for a later phase

The stronger model used by RISE itself in `risechain/xlp-contracts`, referenced by RIP-1 `BaseSubAccount`, is a contract-based adapter/subaccount pattern with explicit owner/manager access control and EIP-1271 signature validation.

TRAXION records that pattern as the preferred direction for a later least-privilege phase because it can move the authorization boundary from a broadly capable externally owned session key to a purpose-built contract boundary.

This direction is **not a prerequisite for the current RISEx testnet integration phase** and does not authorize mainnet rollout by itself.

## Consequences

### Positive

- The gate reflects the authorization behavior actually observed rather than requiring a property the current dashboard-created signer does not satisfy.
- The residual risk is explicit, bounded and reviewable.
- Permission reduction remains a tracked target instead of being silently abandoned.
- A stronger contract-based least-privilege architecture is documented for a later phase.

### Negative / residual risks

- The session-key permission bitmap is broader than perpetual trading.
- Complete Authorization source/ABI provenance remains unavailable; conclusions rely on pinned runtime evidence, documented interfaces and observed behavior.
- The current safety argument depends on the continued absence of a session-key-authorized fund-movement path.
- A future RISEx endpoint, EIP-712 type or contract upgrade can invalidate the accepted assumption without any TRAXION code change.

## Conditions of invalidation and mandatory operational review

The residual-risk acceptance in this ADR immediately ceases to be valid if any RISEx path is discovered that permits fund movement using a session-key signature or a session-key `VerifyWitness`.

If that condition occurs:

1. the RISEx security gate returns to **BLOCKED**;
2. RISEx write enablement must remain or be set **disabled** until the authorization model is re-reviewed;
3. no workaround may reinterpret `MoveFund=true` as acceptable without a new ADR decision;
4. the newly discovered path must be added to the negative security test set before any subsequent unlock decision.

A mandatory review of this ADR must be completed no later than **2026-12-13**.

The review must be brought forward immediately if RISEx publishes or deploys any of the following before that date:

- new API endpoints capable of fund movement, withdrawal, transfer or collateral movement;
- new EIP-712 typed-data structures;
- changes to `VerifyWitness`, signer permissions, session-key registration or permission mutation;
- changes to the pinned Authorization or Router deployment identity;
- new public contract source/ABI material that changes the current architectural analysis.

**Mainnet gate:** no RISEx mainnet execution may be enabled without an explicit re-evaluation of this ADR against the then-current RISEx contracts, endpoints, typed-data model and signer permission behavior. Testnet acceptance under this ADR is not sufficient evidence for mainnet enablement.

## Related evidence

- `docs/security/risex-testnet-runtime-evidence-2026-09-10.md`
- `docs/superpowers/specs/2026-09-09-risex-follower-integration-design.md`
- `docs/adr/ADR-0001-risex-testnet-verification-on-railway-production.md`
