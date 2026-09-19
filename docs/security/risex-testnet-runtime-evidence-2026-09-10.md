# RISEx testnet runtime deployment evidence — 2026-09-10

**Project:** TRAXION

**Scope:** RISEx Phase 4 deployment identity, signer authorization and signed-order evidence

**Write authorization:** RISEx testnet-only

**Deployment identity preflight:** `PASS`

**Full security gate:** `BLOCKED` — post-revoke rejection and the CopyJob submission path remain unproven

## Purpose

This record preserves the evidence used to identify the exact RISEx testnet deployment and the results of signed tests. It is intentionally separate from application configuration and does not enable RISEx writes in the normal CopyJob path.

Full public contract addresses are retained in the referenced GitHub Actions diagnostic logs and are emitted by the permanent read-only preflight. The historical sections below use shortened addresses plus cryptographic fingerprints so deployment identities are not treated as credentials or hand-maintained runtime configuration.

## Live evidence source

The same-run probes queried:

- `GET https://api.testnet.rise.trade/v1/auth/eip712-domain`
- `GET https://api.testnet.rise.trade/v1/system/config`
- RISE testnet JSON-RPC read methods only (`eth_chainId`, `eth_blockNumber`, `eth_getCode`, `eth_getStorageAt`)

Earlier diagnostic evidence also used read-only `eth_call` to prove the critical Authorization interface behavior.

Evidence CI runs:

- GitHub Actions run `34446451396`: Authorization ABI behavioral evidence
- GitHub Actions run `34446652741`: implementation runtime hashes and Solidity metadata check
- GitHub Actions run `34502155535`: permanent pinned deployment preflight live verification

All temporary network diagnostic tests were removed immediately after evidence capture and are not part of the permanent test suite.

## Permanent pinned deployment preflight — PASS

The permanent TRAXION preflight was executed against the live RISEx testnet deployment through GitHub Actions run `34502155535` on 2026-09-10.

Observed block: `53969018`.

All deployment-identity checks returned `PASS`:

- API/RPC chain identity matched;
- EIP-712 `verifyingContract`, `system.auth` and Authorization code evidence matched;
- `system.router` matched the Router code evidence;
- Authorization proxy and EIP-1967 implementation both had the reviewed runtime bytecode;
- Router proxy and EIP-1967 implementation both had the reviewed runtime bytecode;
- observed canonical fingerprint matched the reviewed pin.

Expected fingerprint:

`764412dd3ebb2ecb2b2878e3318bce39e9593cbd32108ebefa90e20161722e2f`

Observed fingerprint:

`764412dd3ebb2ecb2b2878e3318bce39e9593cbd32108ebefa90e20161722e2f`

Permanent preflight result:

- `verdict = PASS`
- `deployment_identity_verified = true`
- `writes_enabled = false`
- `full_security_gate_passed = false`

This `PASS` applies only to the pinned deployment identity. It does not prove the complete ABI/source provenance or the least-privilege capability of a registered signer.

## Deployment identity observed

- API chain ID: `11155931`
- RPC chain ID: `11155931`
- EIP-712 domain: `RISEx`, version `1`
- Authorization proxy: `0x6DA86F…0eE3`
- Authorization implementation: `0x47253b…1cbc`
- RISExUniversalRouter proxy: `0x980b8621…a20f`
- Router implementation: `0xb5c83f…02ee`
- `system.auth` matched the EIP-712 `verifyingContract`
- `system.router` was present and distinct from OrdersManager

## Runtime bytecode fingerprints

Authorization proxy:

- runtime bytes: `1074`
- keccak256: `0x9ab7e46bbf0ec397d2c506c7a40d987702acddacc54515814a09c1b4ee108560`

Authorization implementation:

- runtime bytes: `15558`
- keccak256: `0x606340bf09f07f3027cad6d40441cee241d2f2fdac990607191a650f2ba9fe16`

Router proxy:

- runtime bytes: `1074`
- keccak256: `0x01aa35922b0ce0f6d3295835b9e8f770b87202d0380468bc8b4b14e6ee179755`

Router implementation:

- runtime bytes: `20742`
- keccak256: `0x6a0791ef13de50d862ee1e2462e3037f2694d695622b5de58de0e6259ce434e9`

Canonical deployment fingerprint used by the TRAXION preflight:

`sha256:764412dd3ebb2ecb2b2878e3318bce39e9593cbd32108ebefa90e20161722e2f`

The fingerprint covers network, chain ID, EIP-712 domain name/version, Authorization proxy/implementation identities and runtime code hashes, and Router proxy/implementation identities and runtime code hashes. The block number and ABI-verification status are deliberately excluded so a later read of the unchanged deployment produces the same identity fingerprint.

Any address, proxy implementation or runtime-code change therefore changes the fingerprint and returns the preflight to a blocked state until the new deployment is reviewed and explicitly re-pinned.

## Authorization interface behavioral evidence

The live Authorization implementation contained these selectors and the corresponding `eth_call` view operations succeeded through the proxy:

- `eip712Domain()` → `0x84b0196e`
- `getSessionKeyStatus(address,address)` → `0xdd962cb2`
- `hasPermission(address,address,uint8)` → `0xed82f4b8`
- `registerSigner(address,address,string,uint48,uint8,uint32,bytes)` → `0x8a10cb9e`

Observed read-only behavior:

- `eip712Domain()` returned `RISEx`, version `1`, chain ID `11155931`, and a verifying contract matching the API deployment;
- `getSessionKeyStatus(0x0, 0x0)` returned status `0`;
- `hasPermission(0x0, 0x0, 2)` returned `false`.

This proves runtime interface compatibility for those functions. It does not by itself prove the complete ABI, signer scope, or absence of fund-movement authority.

## ABI/source provenance status

Block explorer source/ABI verification was not available for the observed Authorization and Router implementations. The runtime bytecode also did not expose a recoverable standard IPFS Solidity metadata trailer in the diagnostic check.

Therefore:

- pinned runtime deployment identity: **PASS**;
- critical Authorization interface compatibility: **proven behaviorally**;
- complete ABI/source provenance: **UNKNOWN**;
- signer perps-only/no-fund-movement capability: **FAIL**;
- full RISEx security gate: **BLOCKED**;
- RISEx writes: **disabled**.

The `complete ABI/source provenance` status remains **UNKNOWN** as an external residual risk. Resolution depends on RISEx publishing or verifying the relevant contract source/ABI on the block explorer; it is not a task this project can complete independently.

The `signer perps-only/no-fund-movement capability` status is **FAIL** because the registered dashboard-created testnet signer exposes a fully populated permission bitmap and positive `hasPermission` results for `All`, `Perps`, `Spot` and `MoveFund`. The current risk treatment and revised unlock criterion are defined in `docs/adr/ADR-0002-risex-session-key-authorization-model.md`.

Consequently, any capability conclusion remains bounded by observed runtime behavior, documented interfaces and the residual provenance limitation. Complete source provenance has not been established and must not be inferred from selector compatibility alone.

Run the permanent deployment check from the repository root with:

```bash
python scripts/risex_deployment_preflight.py
```

The command is testnet-only, uses public/read-only API and RPC evidence, and contains no signer secret or transaction path.

## Re-verification — 2026-09-11

A second read-only deployment verification was executed locally from the workstation with:

```bash
python3 scripts/risex_deployment_preflight.py
```

This was a **local execution**, not a GitHub Actions run, so there is no GitHub Actions run ID associated with this evidence.

Observed block: `54066868`  
Previous observed block: `53969018`  
Delta: `97850` blocks.

Result:

- `verdict = PASS`
- deployment checks: `6/6 PASS`
- expected fingerprint: `764412dd3ebb2ecb2b2878e3318bce39e9593cbd32108ebefa90e20161722e2f`
- observed fingerprint: `764412dd3ebb2ecb2b2878e3318bce39e9593cbd32108ebefa90e20161722e2f`
- `deployment_identity_verified = true`
- `writes_enabled = false`
- `full_security_gate_passed = false`

This is the first independent confirmation that the pinned deployment fingerprint remains stable across reads separated in time and by `97850` blocks. That stability is consistent with the deliberate exclusion of the block number from the canonical deployment fingerprint: the deployment identity remains unchanged while the chain advances.

## Signer permission evidence — 2026-09-13

Read-only signer-permission evidence collected on 2026-09-12/13 against the pinned RISEx testnet Authorization deployment.

Deployment evidence:

- Authorization proxy: `0x6DA86F486b5E6536358F5b122dBe184522CA0eE3`
- `GET /v1/system/config` confirmed the pinned proxy as `system.auth`
- Authorization implementation: `0x47253b880ec432a32485e30510c8eb6afe721cbc`
- implementation runtime size: `15558` bytes

Selectors verified present in the runtime bytecode:

- `enablePermission` → `0x77007534`
- `disablePermission` → `0x06991816`
- `sessionKeys` → `0x96ade1f9`
- `getSessionKeyStatus` → `0xdd962cb2`
- `hasPermission` → `0xed82f4b8`
- `registerSigner` → `0x8a10cb9e`

Raw state observed for the session key created through the RISEx testnet dashboard:

- `sessionKeys.permissionBitmap = 4294967295`
- `sessionKeys.permissionBitmap_hex = 0xFFFFFFFF`
- `sessionKeys.status = 1`
- `hasPermission(All) = true`
- `hasPermission(Perps) = true`
- `hasPermission(Spot) = true`
- `hasPermission(MoveFund) = true`

Interpretation:

- the signer is active;
- perpetual-trading permission is present;
- the observed Authorization state is broader than perps-only and includes `MoveFund`;
- the RISEx UI statement that API wallets cannot withdraw funds therefore diverges from the Authorization permission state observed on-chain;
- this evidence proves the permission-model divergence, but does not by itself prove a usable session-key fund-movement path.

Security status:

- signer perps-only/no-fund-movement capability: **FAIL**;
- full RISEx security gate: **BLOCKED** pending application of the revised authorization model;
- RISEx writes: **disabled**;
- residual-risk analysis and the revised unlock criterion are governed by `docs/adr/ADR-0002-risex-session-key-authorization-model.md`.

## First signed order — 2026-09-19

A signed RISEx testnet POST produced a fully filled order against the deployment pinned by ADR-0005. This is an execution result, not a security-gate `PASS`.

Deployment evidence:

- pinned deployment fingerprint: `443ba3af37d36e5ab044ab68d1af8b05b0372fd05987c41a3121cc628b18d2bd`;
- deployment decision: `docs/adr/ADR-0005-risex-testnet-deployment-repin.md`;
- deployment preflight: `PASS` (`6/6` checks).

Submitted order:

- transaction hash: `0xe4254e4bb7371ff404b87dbecdcc7a3076da19097cea9b3e67e983586c2dd1f7`;
- `sc_order_id = 224552`;
- order ID: `0x0000000000036d28000000000343672100000000000001f3`;
- block: `54748961`;
- market: `BTC/USDC`, `market_id = 1`;
- side/type/time in force: `BUY LIMIT IOC`;
- `size_steps = 100`, equal to `0,0001 BTC`;
- `price_ticks = 815949`;
- `client_order_id = 11892285924151961225`;
- `filled_percent = 100.00`;
- terminal message: `Order fully filled`;
- `GET /v1/tx/{hash}`: `success = true`, `error = null`.

### Nonce consumption — behavioral evidence of consumption

Nonce state before submission:

- `anchor = 1`;
- `current_bitmap_index = 2`;
- `bitmap = 0x3`.

Nonce state after submission:

- `anchor = 1`;
- `current_bitmap_index = 3`;
- `bitmap = 0x7`.

Bit `2`, used by the permit, changed from unset to consumed. This is the first **behavioral** evidence that a successfully submitted signed order consumes the selected nonce. It is distinct from the architectural replay-protection attestation produced by the collector.

This evidence proves nonce consumption for this successful submission. It does **not** prove that the provider rejects a replay. That proof requires a second POST using the same permit: if replay protection works, no order is placed; if it does not, a second order can be placed. That negative test was not attempted and requires separate explicit operator authorization. It also does not replace the pending post-revoke order-rejection test.

### Active replay prerequisite was not satisfied before the POST

At the time of the first positive POST, replay protection was supported only by the collector's **architectural** attestation. The behavioral prerequisite in ADR-0001, lines 25–30 — rejection of a replayed signed request before the first positive order — had not been satisfied. The successful POST therefore occurred without satisfying that active prerequisite.

ADR-0002 supersedes ADR-0001 only with respect to the erroneous execution-environment premise; it explicitly carries the isolation requirement forward and does not supersede the independent replay prerequisite. ADR-0003 made `fund_movement_rejected` and `withdrawal_rejected` conditionally applicable, but did not change replay evidence: replay was expressly outside that ADR's scope, and its decision states that independent replay controls are not weakened.

The exposed perimeter of this non-conforming execution was:

- RISEx testnet;
- a disposable account;
- one `0,0001 BTC` order, approximately USD 8 notional;
- a revocable signer;
- capital confined by the bounded-capital requirement in ADR-0002.

These facts define what was exposed. They do not minimize, waive or retroactively satisfy the unmet replay prerequisite.

### Three fail-closed blocks observed before the successful POST

The signed-order path was blocked three times before the successful submission:

1. **Deployment drift:** RISEx updated the Authorization and Router implementations while preserving the proxies. The pinned preflight blocked the write until the new deployment was reviewed and recorded in ADR-0005.
2. **Freshness evidence unavailable:** the freshness probe used `collect_public_signer_evidence`, which could not determine `session_active`. PR #183 changed the path to use RPC and `collect_authorization_session_evidence`.
3. **Nonce bitmap semantic mismatch:** the anti-replay collector compared the on-chain bitmap with `current_bitmap_index` instead of comparing bitmap values. PR #184 corrected the semantic comparison.

All three blocks occurred in previously unexecuted code paths and demonstrate that the gates failed closed before the first successful POST.

### Security gate status after this evidence

| Gate evidence | Status |
|---|---|
| Pinned deployment identity | **PASS** — ADR-0005 fingerprint, preflight `6/6` |
| Signed-order POST outcome | **SUCCEEDED** — testnet IOC fully filled; factual execution result, not a security-gate `PASS` |
| Replay-protection architecture | **ATTESTED ONLY** — collector evidence, not behavioral replay rejection |
| Nonce consumption after signed submission | **OBSERVED** — bitmap changed from `0x3` to `0x7`; proves consumption only |
| ADR-0001 replay prerequisite before first positive POST | **NOT SATISFIED** — behavioral replay rejection was not proven before the POST |
| Replayed payload rejection | **NOT YET PROVEN** |
| `post_revoke_order_rejected` | **NOT YET PROVEN** |
| RISEx CopyJob submission path | **NOT CONNECTED** |
| Full RISEx security gate | **BLOCKED** |

The successful testnet order and nonce-consumption evidence do not authorize normal RISEx writes, do not connect RISEx to the CopyJob worker, and do not establish that the full security gate has passed.
