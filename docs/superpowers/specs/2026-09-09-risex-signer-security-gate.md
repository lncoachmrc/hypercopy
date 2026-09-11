# RISEx Signer Security Evidence Gate

**Date:** 2026-09-09  
**Project:** TRAXION  
**Scope:** RISEx follower execution authorization  
**Deployment identity preflight:** `PASS` on reviewed RISEx testnet pin  
**Full security gate:** `BLOCKED / UNKNOWN`

## Purpose

TRAXION may store and use only a dedicated, revocable execution credential that is demonstrably incapable of moving user funds. For RISEx, a signer is acceptable only if independent evidence proves that the exact deployed authorization path limits that signer to perpetual-trading actions and cannot be used to transfer, withdraw or otherwise move funds.

This document defines the evidence required to move the RISEx write gate from `UNKNOWN` to `PASS`. A `PASS` report is evidence only: it does **not** enable RISEx writes, change `ENABLE_LIVE_TRADING`, change the database `live_trading` flag, change a user's copy state, deploy code, or authorize mainnet.

## Current Evidence Limitation

The official RISEx signer-registration reference documents account, signer, message, expiration, bitmap nonce state, account/signer EIP-712 signatures and an optional label. It does not document a client-selected granular permission/scope field such as `PERPS` during registration. Therefore TRAXION does not infer least privilege from an API field named `permission` or `permissions`.

The deployed testnet Authorization contract has now been behaviorally verified to expose `hasPermission(address,address,uint8)`, and the public RISE integration checks permission ID `2` as `Perps` after signer registration. This is meaningful evidence for the contract model, but it is not yet proof that a real registered signer lacks every fund-movement capability.

The official order flow uses a separate EIP-712 `VerifyWitness` containing `account`, `target`, `hash`, bitmap nonce state and deadline. For perpetual orders, `target` is the runtime `RISExUniversalRouter` and the action hash is built from a perps-specific selector such as `RISE_PERPS_PLACE_ORDER_V1`.

The security question is therefore the **effective scope of the deployed authorization path**. The Phase 4 gate must prove from the exact deployed contracts and controlled testnet behavior that a registered signer cannot authorize a non-perpetual/fund-movement action through the router or another supported target.

RISEx order documentation also exposes a JWT/OperatorHub authorization route in addition to signer/permit flows. The TRAXION trading worker is therefore constrained to the `registered_signer_permit` authorization mode and must remain unable to use JWT/OperatorHub as a bypass.

Relevant public references:

- `https://developer.rise.trade/reference/authservice_registersigner`
- `https://developer.rise.trade/reference/orderservice_placeorder`
- `https://developer.rise.trade/reference/javascripttypescript`
- `https://developer.rise.trade/reference/general-information`

## Pinned Runtime Deployment Preflight

TRAXION now has a permanent testnet-only deployment identity preflight that runs before any future signer-security test can be considered.

Reviewed testnet deployment fingerprint:

`764412dd3ebb2ecb2b2878e3318bce39e9593cbd32108ebefa90e20161722e2f`

The canonical fingerprint covers:

- network;
- chain ID;
- EIP-712 domain name/version;
- Authorization proxy address and runtime-code hash;
- Authorization EIP-1967 implementation address and runtime-code hash;
- Router proxy address and runtime-code hash;
- Router EIP-1967 implementation address and runtime-code hash.

The observed block number is evidence metadata and is excluded from the fingerprint. ABI-verification status is also excluded because deployment identity and full ABI/source provenance are separate checks.

The permanent collector performs only:

- HTTP `GET /v1/auth/eip712-domain`;
- HTTP `GET /v1/system/config`;
- JSON-RPC `eth_chainId`;
- JSON-RPC `eth_blockNumber`;
- JSON-RPC `eth_getCode`;
- JSON-RPC `eth_getStorageAt`.

All proxy/implementation reads are pinned to the same observed block. The RPC transport rejects every method outside that explicit allowlist before network I/O. It therefore cannot submit a transaction or execute an arbitrary `eth_call`.

Run from the repository root:

```bash
python scripts/risex_deployment_preflight.py
```

The CLI is testnet-only. It offers no private-key, signature, token, bearer, JWT, API-key or OperatorHub input.

Preflight semantics:

- exact identity + code fingerprint match → `PASS` for **deployment identity only**;
- address/implementation/code drift → `FAIL` and remain blocked;
- incomplete/malformed/unavailable evidence → `UNKNOWN` and remain blocked.

Live permanent-preflight verification on 2026-09-10, GitHub Actions run `34502155535`, observed block `53969018`:

- chain identity: `PASS`;
- Authorization identity: `PASS`;
- Router identity: `PASS`;
- Authorization proxy/implementation code: `PASS`;
- Router proxy/implementation code: `PASS`;
- deployment fingerprint: `PASS`;
- `deployment_identity_verified = true`;
- `writes_enabled = false`;
- `full_security_gate_passed = false`.

A deployment identity `PASS` is a prerequisite, not authorization to write. Any RISEx contract upgrade changes the fingerprint and forces review/re-pinning before subsequent security work.

The retained evidence is in `docs/security/risex-testnet-runtime-evidence-2026-09-10.md`.

## Verdict Semantics

### PASS

Full signer-security `PASS` is allowed only when every required check is explicitly proven:

1. **Deployment identity**
   - permanent pinned deployment preflight is `PASS`;
   - chain ID is known;
   - runtime Authorization/verifying contract and Router are identified;
   - proxy implementation and runtime code match the reviewed deployment.

2. **Session state**
   - signer is active;
   - signer is bound to the expected account;
   - signer is not expired.

3. **On-chain authorization scope proof**
   - exact deployed Authorization contract/router path is independently inspected;
   - effective scope for the registered signer is proven to permit the intended perpetual execution;
   - no supported target/action path lets that signer authorize transfer, withdrawal, collateral movement, spot movement or another fund-moving action;
   - provider/API permission labels are not accepted as independent proof.

4. **Controlled positive perpetual test**
   - the same signer can perform the intended perpetual execution action on testnet.

5. **Controlled negative fund-movement test**
   - a fund-movement attempt made with the same signer is rejected by RISEx authorization itself.

6. **Controlled negative withdrawal test**
   - a withdrawal attempt made with the same signer is rejected by RISEx authorization itself.

7. **Revocation test**
   - after signer revocation, a new perpetual action using that signer is rejected.

8. **Authorization-path isolation**
   - TRAXION trading worker cannot use JWT/OperatorHub or another route to bypass the dedicated signer.

### FAIL

Any proven unsafe condition makes the full gate `FAIL`, including:

- deployment fingerprint drift that has not been reviewed/re-pinned;
- signer inactive or revoked when expected active;
- signer bound to another account;
- signer expired;
- deployed authorization path gives the registered signer broader-than-perps authority;
- a fund-moving target/action can be authorized by the same signer;
- controlled perpetual action fails;
- fund movement or withdrawal is not rejected by RISEx authorization;
- an order still succeeds after revocation;
- an alternate JWT/OperatorHub bypass remains reachable from the trading worker.

### UNKNOWN

Missing or ambiguous evidence is `UNKNOWN`, never `PASS`. Examples:

- deployment preflight cannot acquire complete same-run evidence;
- numeric signer status with no documented semantic mapping;
- API-provided `permission`/`permissions` labels without independent contract proof;
- inability to establish the effective target/action scope of `VerifyWitness` for a registered signer;
- behavioral positive/negative tests not yet run;
- inability to prove that the worker cannot use an alternate authorization path;
- provider read failure or malformed response.

`UNKNOWN` keeps the RISEx write gate blocked.

## Implemented Public Read-Only Signer Probe

The signer probe collects only public evidence with HTTP `GET` requests:

- `/v1/auth/eip712-domain`
- `/v1/system/config`
- `/v1/auth/session-key-status`
- `/v1/auth/signers`

The HTTP transport rejects every `POST` locally before network I/O. The RISEx adapter independently rejects all mutating methods while `writes_enabled` remains statically `False`.

The public collector deliberately ignores any provider-supplied `permission` or `permissions` label. It reports `onchain_perps_only_scope = null` until a separate independent on-chain inspection plus controlled signer behavior proves the effective scope of the exact deployment.

Run from the repository root using only public addresses:

```bash
python scripts/risex_signer_probe.py \
  --network testnet \
  --account 0x1111111111111111111111111111111111111111 \
  --signer 0x2222222222222222222222222222222222222222
```

The command intentionally has no private-key, seed, password, token or signature option.

Exit codes for security probe/preflight commands:

- `0`: `PASS`
- `1`: `FAIL`
- `2`: `UNKNOWN` or provider read unavailable

With public signer API evidence alone, the expected safe signer-gate result is `UNKNOWN` because controlled on-chain scope and behavioral tests are intentionally not supplied by the read-only collector.

## Evidence Report Rules

A retained security-evidence report may contain only public/non-secret material such as network/chain ID, account address, signer public address, contract identities/code hashes, expiration/status evidence, independently derived authorization-scope results, future controlled testnet transaction hashes/revert reasons, probe verdicts and exact TRAXION/RISEx versions.

It must never contain seed phrases, main-wallet private keys, signer private keys, reusable signature material, session cookies/tokens, bearer/JWT authorization or other secrets.

## Separate Authorization Required Before Any Signed Test

The current foundation and deployment preflight do not register a signer, persist a RISEx signer private key, place/cancel an order, update leverage, move funds or perform a withdrawal.

Controlled signed testnet verification remains a separate safety boundary. Before executing it, the plan must:

1. start from a pinned deployment identity `PASS`;
2. define a disposable dedicated test account/signer and rollback/revoke procedure;
3. prove from the exact deployment whether permission ID `2` and the surrounding Authorization/Router design provide the required scope;
4. map supported `VerifyWitness` targets/action families relevant to fund movement;
5. perform the positive perpetual test;
6. perform negative fund-movement and withdrawal tests;
7. revoke the signer and prove post-revoke rejection;
8. prove JWT/OperatorHub remains unreachable from the TRAXION trading worker;
9. retain only sanitized evidence;
10. require explicit authorization before executing any signed test.

If the deployed contract model provides a registered signer with broader-than-acceptable authority, the correct gate result is `FAIL`; TRAXION must not work around that by trusting application-side intent filtering alone.

Even a successful testnet full-gate `PASS` does not authorize mainnet. The complete evidence gate must be repeated against the exact RISEx mainnet deployment before any mainnet approval can be considered.
