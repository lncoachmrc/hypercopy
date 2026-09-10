# RISEx testnet runtime deployment evidence — 2026-09-10

**Project:** TRAXION  
**Scope:** read-only RISEx Phase 4 deployment identity  
**Write authorization:** none  
**Security gate:** `UNKNOWN / BLOCKED`

## Purpose

This record preserves the read-only evidence used to identify the exact RISEx testnet deployment before any signed test is considered. It is intentionally separate from application configuration and does not enable RISEx writes.

Full public contract addresses are retained in the referenced GitHub Actions diagnostic logs. This document uses shortened addresses plus cryptographic fingerprints so the repository does not treat deployment addresses as credentials or runtime configuration.

## Live evidence source

The same-run probes queried:

- `GET https://api.testnet.rise.trade/v1/auth/eip712-domain`
- `GET https://api.testnet.rise.trade/v1/system/config`
- RISE testnet JSON-RPC read methods only (`eth_chainId`, `eth_blockNumber`, `eth_getCode`, `eth_getStorageAt`, `eth_call`)

Diagnostic CI runs:

- GitHub Actions run `34446451396`: Authorization ABI behavioral evidence
- GitHub Actions run `34446652741`: implementation runtime hashes and Solidity metadata check

The temporary network diagnostic test used to collect this evidence was removed immediately afterwards and is not part of the permanent test suite.

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

Canonical deployment fingerprint used by the TRAXION preflight design:

`sha256:764412dd3ebb2ecb2b2878e3318bce39e9593cbd32108ebefa90e20161722e2f`

The fingerprint covers network, chain ID, EIP-712 domain name/version, Authorization proxy/implementation identities and runtime code hashes, and Router proxy/implementation identities and runtime code hashes. The block number is evidence metadata and is deliberately excluded so a later read of the unchanged deployment can match the same fingerprint.

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

- runtime deployment identity: **proven for the observed run**;
- critical Authorization interface compatibility: **proven behaviorally**;
- complete ABI/source provenance: **UNKNOWN**;
- signer perps-only/no-fund-movement capability: **UNKNOWN**;
- RISEx write gate: **BLOCKED**.

Any contract upgrade or deployment fingerprint mismatch must return the deployment preflight to a blocked state until the new deployment is reviewed and re-pinned.
