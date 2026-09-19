# ADR-0005 — RISEx testnet deployment re-pin

- **Status:** Proposed
- **Date:** 2026-09-19
- **Scope:** TRAXION / RISEx testnet deployment identity re-review and controlled re-pin
- **Decision owner:** TRAXION project owner
- **Related decisions:** ADR-0002 — RISEx session-key authorization model; ADR-0003 — RISEx fund-movement negative-probe applicability; ADR-0004 — RISEx continuous execution authorization
- **Mandatory review date:** 2026-12-13

## Context

TRAXION pins the reviewed RISEx testnet deployment identity through a canonical fingerprint derived from the security-relevant runtime identity of the Authorization and Router contracts.

The pin is intentionally fail-closed. Any change to an implementation address or implementation runtime-code hash changes the canonical fingerprint and causes the permanent deployment preflight to fail until the changed deployment is reviewed explicitly.

On 2026-09-19, that control detected a RISEx testnet deployment change before the first signed order was permitted to proceed.

All evidence summarized in this ADR was collected **read-only**. No provider order, signature submission, contract transaction, write-capable RPC method or Railway configuration change was required for the re-review.

## Triggering event

The previously reviewed deployment fingerprint was:

`764412dd…`

A subsequent read-only preflight observed a different deployment identity and returned:

- `verdict = FAIL`;
- process exit code `1`;
- `deployment_identity_verified = false`.

The reviewed new canonical deployment fingerprint is:

`443ba3af37d36e5ab044ab68d1af8b05b0372fd05987c41a3121cc628b18d2bd`

This exact reviewed fingerprint is the value that must be applied by the separate implementation PR that updates `PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT`. This ADR records the complete trust-anchor value and does not modify the runtime constant.

The failure was caused by EIP-1967 implementation upgrades behind otherwise stable proxy identities. The read-only deployment evidence was observed at block `54740392`.

## Read-only evidence collected on 2026-09-19

### Observation block

The deployment re-review evidence summarized below was observed at block:

`54740392`

### Stable proxy identities

The reviewed proxy identities remained unchanged:

- Authorization proxy: `0x6DA86F…0eE3`;
- Router proxy: `0x980b86…a20f`.

The EIP-712 Authorization verifying contract remained the same Authorization proxy.

### Authorization implementation change

Previous reviewed Authorization implementation:

`0x47253b…1cbc`

New observed Authorization implementation:

`0x4ea534…5e6f`

Runtime implementation size changed from:

- previous: `15558` bytes;
- new: `19949` bytes;
- delta: `+4391` bytes, approximately `+28%`;
- implementation runtime code keccak256: `0x7ce281c4192e00cc73238e560429ed4a084b1a098b0503f8bfa77c7b9cb7d698`.

### Router implementation change

Previous reviewed Router implementation:

`0xb5c83f…02ee`

New observed Router implementation:

`0x5607dd…6217`

Runtime implementation size changed from:

- previous: `20742` bytes;
- new: `25743` bytes;
- delta: `+5001` bytes, approximately `+24%`;
- implementation runtime code keccak256: `0x1acd9cf57565bf4239ae9061a62251ec396024b14172e3a2e21ae8bf0a017652`.

### Fingerprint change

The canonical deployment fingerprint changed from:

`764412dd…`

to:

`443ba3af37d36e5ab044ab68d1af8b05b0372fd05987c41a3121cc628b18d2bd`

This is the expected behavior of the TRAXION deployment pin: implementation-address or implementation-runtime changes invalidate the previously reviewed deployment identity even when the proxies themselves remain stable.

### EIP-712 domain remained compatible

Read-only `GET /v1/auth/eip712-domain` returned:

- name: `RISEx`;
- version: `1`;
- chain ID: `11155931`;
- verifying contract: unchanged Authorization proxy.

No domain drift was observed.

### VerifyWitness typehash remained compatible

A read-only `eth_call` to `VERIFY_WITNESS_TYPEHASH()` returned:

`0x055e6bcb…16821`

This is identical to the `VerifyWitness` typehash used by the TRAXION signing path.

Therefore the reviewed upgrade did not change the EIP-712 `VerifyWitness` signing schema used by the current integration.

### Authorization selector compatibility

Nine security-relevant selectors were verified present in the new Authorization implementation bytecode.

Legacy session-key and permission surface:

- `enablePermission`;
- `disablePermission`;
- `sessionKeys`;
- `getSessionKeyStatus`;
- `hasPermission`;
- `registerSigner`.

Replay-protection surface introduced into the TRAXION review model:

- `isNonceUsed`;
- `getNonceState`;
- `VERIFY_WITNESS_TYPEHASH`.

All nine were present in the reviewed runtime.

Selector presence establishes interface compatibility for the reviewed surface. It does not establish full source equivalence or prove the semantics of unrelated added bytecode.

### Existing signer state survived the upgrade

The previously registered TRAXION RISEx testnet signer remained live after the implementation change.

Read-only state observed after the upgrade:

- expiration: `1791787206`;
- permission bitmap: `0xFFFFFFFF`;
- status: `1`.

This state was unchanged relative to the pre-upgrade signer evidence.

The persistence of the existing signer state is consistent with storage compatibility across the proxy implementation upgrade.

It does not expand the authorization conclusion of ADR-0002: the broad permission bitmap and the residual fund-movement analysis remain governed by ADR-0002 and ADR-0003.

## What this review established

The 2026-09-19 read-only review established the following limited compatibility facts:

1. the testnet Authorization and Router proxies remained unchanged;
2. both EIP-1967 implementation addresses changed;
3. both implementation runtimes increased materially in size;
4. the API/RPC deployment identity no longer matched the old pin;
5. the EIP-712 domain remained `RISEx`, version `1`, chain ID `11155931`, with the same Authorization verifying contract;
6. the `VerifyWitness` typehash remained identical to the value used by TRAXION;
7. all nine reviewed Authorization selectors remained present;
8. the existing test signer remained active with the same expiration, permission bitmap and status;
9. the observed deployment is compatible with the specific Authorization/session/replay surfaces required by the current TRAXION RISEx testnet integration.

These facts are sufficient for the limited testnet re-pin decision below, subject to the residual risk inherited from ADR-0002.

## What this review did not establish

The review **did not establish what the additional implementation bytecode does**.

RISEx does not provide verified source for the reviewed implementation on the block explorer, so the additional Authorization and Router bytecode cannot be mapped comprehensively to reviewed Solidity source.

Accordingly, the existing ADR-0002 residual-risk finding is materialized by this upgrade:

> complete ABI/source provenance: **UNKNOWN**

The presence of the required selectors, unchanged domain/typehash and preserved signer state proves compatibility for the reviewed integration surface. It does **not** prove that the new implementations are behaviorally identical to the previous implementations outside that surface.

In particular, this ADR does not claim:

- complete semantic equivalence of the old and new implementations;
- absence of new callable functions or hidden behavior;
- complete ABI equivalence;
- source-level equivalence;
- absence of newly introduced fund-movement or privilege paths beyond the specifically reviewed evidence;
- mainnet equivalence.

This residual risk is accepted only inside the constrained testnet perimeter defined below.

## Provider-change transparency

No public announcement specific to this implementation upgrade was identified during the review.

The review found:

- no provider changelog entry describing the Authorization/Router implementation change;
- no public status-page notice for the upgrade;
- no indexed public announcement identifying the implementation transition.

This statement is limited to publicly available and indexed material reviewed by TRAXION. It does not assert that no message existed in a private or non-indexed communication channel.

The absence of a public upgrade notice is security-relevant operational information because TRAXION had to detect the deployment change independently through runtime identity verification.

## Security-control finding

The deployment pin performed its intended security function.

Without the reviewed deployment fingerprint, the first signed testnet order could have proceeded against a contract implementation different from the one TRAXION had reviewed.

Instead:

1. the implementation changed;
2. the canonical fingerprint changed;
3. the preflight returned `FAIL`;
4. `deployment_identity_verified` became false;
5. signed execution remained blocked pending explicit re-review.

The pin is therefore not documentation metadata. It is an active security boundary against silent provider upgrades.

## Decision

### 1. Approve a testnet-only re-pin after this review is accepted

TRAXION accepts the reviewed RISEx testnet deployment identity represented by fingerprint:

`443ba3af37d36e5ab044ab68d1af8b05b0372fd05987c41a3121cc628b18d2bd`

for the **testnet verification perimeter only**.

The implementation PR that follows this ADR may update:

`PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT`

from the previous reviewed fingerprint to the full reviewed `443ba3af37d36e5ab044ab68d1af8b05b0372fd05987c41a3121cc628b18d2bd` fingerprint.

This ADR itself does not change the constant.

### 2. Preserve the ADR-0002 constrained-risk perimeter

The re-pin is valid only under the controls already required by ADR-0002, including:

- disposable/test-only account;
- capital confined to the testnet verification perimeter;
- dedicated and revocable signer;
- isolated verification environment;
- no reuse of production/mainnet credentials;
- no implication that the signer is safe for unrestricted custody or fund movement;
- all independent readiness, replay, deployment, account/signer binding and point-of-use controls remain mandatory.

The re-pin acknowledges the `complete ABI/source provenance: UNKNOWN` residual risk. It does not remove or downgrade it.

### 3. No mainnet authorization

This decision applies only to the reviewed RISEx **testnet** deployment.

It does not:

- approve any RISEx mainnet deployment;
- authorize production/mainnet trading;
- establish identity or equivalence of any mainnet Authorization or Router implementation;
- permit reuse of the testnet fingerprint on another network;
- relax TRAXION's explicit production/mainnet authorization requirements.

Any mainnet RISEx deployment requires its own deployment identity, security review and explicit decision.

### 4. Re-pin is manual and review-gated

The deployment fingerprint must never be updated automatically in response to a provider upgrade.

A fingerprint mismatch is a security event requiring:

1. fail-closed preflight;
2. read-only identification of the changed runtime;
3. explicit technical re-review;
4. documentation of compatibility findings and residual risks;
5. explicit project-owner decision;
6. a separate reviewed PR that changes the pin.

Automation may collect evidence. It must not approve or persist a new trust anchor.

## Invalidation conditions

Acceptance of the reviewed testnet fingerprint expires immediately if any of the following occurs:

- Authorization proxy changes;
- Authorization implementation changes again;
- Authorization implementation runtime code hash changes;
- Router proxy changes;
- Router implementation changes again;
- Router implementation runtime code hash changes;
- EIP-712 domain name changes;
- EIP-712 domain version changes;
- EIP-712 chain ID changes;
- EIP-712 verifying contract changes;
- `VERIFY_WITNESS_TYPEHASH()` changes;
- the reviewed `VerifyWitness` schema no longer matches the runtime typehash;
- any currently required Authorization selector disappears or materially changes;
- signer/session semantics change in a way that invalidates ADR-0002/ADR-0003 assumptions;
- verified source/ABI material later contradicts the assumptions accepted by this review.

On any invalidation event:

1. deployment preflight must return to `FAIL` or otherwise block signed execution;
2. the old re-pin must not be carried forward automatically;
3. signed RISEx execution must remain blocked;
4. a new explicit technical review is required;
5. a new fingerprint may be accepted only through another human-reviewed decision and implementation PR.

The re-pin must never be self-healing or self-updating.

## Status transition

This ADR remains **Proposed** in this documentation-only PR.

It moves to **Accepted** only in the separate PR that actually updates `PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT` to the full reviewed `443ba3af37d36e5ab044ab68d1af8b05b0372fd05987c41a3121cc628b18d2bd` fingerprint.

That implementation PR must preserve the same separation already used by ADR-0003 and ADR-0004:

- decision first;
- implementation separately;
- no runtime change hidden inside the decision-only PR.

If the implementation PR does not apply exactly the reviewed fingerprint or introduces additional unrelated authorization changes, this ADR must remain Proposed until those differences are reviewed.

## Mandatory review date

This ADR must be reviewed no later than **2026-12-13**.

The date is intentionally aligned with ADR-0002, ADR-0003 and ADR-0004 because the accepted residual risks and authorization assumptions are coupled.

Any invalidation condition above requires an earlier review.

## Consequences

### Positive

- A silent provider implementation upgrade is captured as an explicit trust-boundary event.
- Testnet verification can resume only after a human-reviewed re-pin.
- Domain, typehash, selector and signer-state compatibility are recorded independently from the fingerprint itself.
- The operational value of the deployment pin is demonstrated by a real detected upgrade.
- The decision preserves the distinction between interface compatibility and complete source/ABI provenance.

### Negative / residual risks

- The added bytecode cannot be fully interpreted without verified source.
- Complete ABI/source provenance remains `UNKNOWN`.
- Selector compatibility cannot prove absence of additional behavior.
- RISEx did not provide a publicly identified upgrade notice in the reviewed sources.
- Future provider upgrades can invalidate this decision without any TRAXION code change.
- A separate implementation PR is still required before the new deployment can satisfy the permanent pin.

## Implementation boundary

This ADR PR is **documentation-only**.

It must not modify:

- `PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT`;
- any Authorization or Router address;
- RISEx adapters, transports, signing or order-preparation code;
- pre-order or readiness gates;
- tests or fixtures;
- Railway configuration;
- environment variables;
- write-enable flags;
- deployment state.

The follow-up implementation PR is limited to applying the reviewed testnet fingerprint and transitioning this ADR from `Proposed` to `Accepted`, together with the tests necessary to prove that the permanent preflight accepts exactly the newly reviewed deployment and remains fail-closed for drift.

## Related evidence and decisions

- `docs/adr/ADR-0002-risex-session-key-authorization-model.md`
- `docs/adr/ADR-0003-risex-fund-movement-negative-probe-applicability.md`
- `docs/adr/ADR-0004-risex-continuous-execution-authorization.md`
- `docs/security/risex-testnet-runtime-evidence-2026-09-10.md`
- `backend/app/security/risex_deployment_runtime.py`
- `backend/app/security/risex_deployment_preflight.py`
- `backend/app/security/risex_replay_protection_architecture.py`
