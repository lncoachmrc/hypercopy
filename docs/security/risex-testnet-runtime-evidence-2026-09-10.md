# RISEx testnet runtime deployment evidence — 2026-09-10

**Project:** TRAXION  
**Scope:** read-only RISEx Phase 4 deployment identity  
**Write authorization:** none  
**Deployment identity preflight:** `PASS`  
**Full security gate:** `UNKNOWN / BLOCKED`

## Purpose

This record preserves the read-only evidence used to identify the exact RISEx testnet deployment before any signed test is considered. It is intentionally separate from application configuration and does not enable RISEx writes.

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
- signer perps-only/no-fund-movement capability: **UNKNOWN**;
- full RISEx security gate: **BLOCKED**;
- RISEx writes: **disabled**.

Run the permanent deployment check from the repository root with:

```bash
python scripts/risex_deployment_preflight.py
```

The command is testnet-only, uses public/read-only API and RPC evidence, and contains no signer secret or transaction path.
