# RISEx Testnet Security Gate — Phase 4 Plan

**Date:** 2026-09-09  
**Project:** TRAXION  
**Branch:** `feature/risex-testnet-security-gate`  
**Stacked on:** `feature/risex-readonly-foundation` @ `0a68169930e29329d0426639a86f882c7fbcac97`  
**Current execution status:** RISEx writes disabled; gate `BLOCKED / UNKNOWN`

## Goal

Determine, from public/read-only evidence first, whether the exact RISEx testnet authorization architecture can satisfy TRAXION's non-custodial least-privilege requirement for a dedicated execution signer.

The gate must establish whether a registered RISEx signer is effectively restricted to perpetual execution and cryptographically unable to authorize transfer, withdrawal, collateral movement or another fund-moving action.

## Hard Safety Constraints

This Phase 4 branch starts in **read-only mode**.

Until a separate explicit authorization is given for signed testnet verification:

- do not register or revoke a signer;
- do not place, cancel or modify an order;
- do not update leverage;
- do not attempt a transfer, withdrawal or collateral movement;
- do not store or request a private key, seed phrase, reusable signature, JWT, bearer token or session cookie;
- do not change `RISExAdapter.writes_enabled` from `False`;
- do not change `ENABLE_LIVE_TRADING`, database `live_trading`, user copy state or execution network;
- do not deploy or mutate Railway production;
- do not use mainnet.

A future signed test harness must remain disabled by default and must require a new, explicit approval before execution.

## Phase A — Exact Testnet Deployment Discovery (read-only)

1. Query the current RISEx testnet public endpoints:
   - `/v1/auth/eip712-domain`;
   - `/v1/system/config`.
2. Capture only public evidence:
   - API base URL;
   - chain ID;
   - Authorization/verifying-contract address;
   - Universal Router address;
   - probe timestamp;
   - exact TRAXION commit.
3. Validate each returned address as an actual contract using a read-only RPC/explorer query.
4. Obtain verified source/ABI when available. If source/ABI cannot be independently established, retain `UNKNOWN`; do not infer semantics from names or API labels.
5. Record deployment identity in a sanitized evidence artifact.

**Exit condition:** exact deployment identity established or gate remains `UNKNOWN`.

## Phase B — Authorization Scope Analysis (read-only)

1. Inspect the exact deployed registration and authorization path used by:
   - `RegisterSigner`;
   - `VerifyWitness`;
   - `RISExUniversalRouter`.
2. Confirm the current documented registration tuple contains account, signer, message, expiration and nonce/signatures, with no assumed granular permission field.
3. Trace how `VerifyWitness.target` and `VerifyWitness.hash` are validated for a registered signer.
4. Map every router target/action family that the same signer could authorize, including any operation capable of:
   - moving collateral;
   - transferring assets;
   - withdrawing assets;
   - spot/fund movement;
   - changing account authority.
5. Classify the effective signer scope:
   - `True`: independently proven perps-only;
   - `False`: broader-than-perps authorization exists;
   - `None`: proof is incomplete or ambiguous.
6. If broader authority is proven, stop the gate with `FAIL`. Application-side intent filtering is not an acceptable substitute for cryptographic least privilege.

**Exit condition:** `onchain_perps_only_scope` is independently determined as `True`, `False`, or remains `None` with explicit missing evidence.

## Phase C — TRAXION Authorization-Path Isolation (no provider writes)

1. Add static and unit tests proving the RISEx execution path cannot:
   - attach `Authorization: Bearer ...`;
   - consume a JWT/OperatorHub credential;
   - fall back to an alternate provider authorization path;
   - read the main-wallet private key.
2. Keep all current mutating RISEx adapter methods fail-closed.
3. Prove the read-only transport rejects every POST before network I/O.
4. Make any future signed transport a separate type/module so read-only code cannot silently gain mutation capability.

**Exit condition:** `operatorhub_bypass_disabled` can be supported by code evidence, while provider-side behavioral checks remain unexecuted.

## Phase D — Controlled Signed Test Harness (code only until separately authorized)

Only after Phases A–C are complete and only after explicit approval:

1. Use a disposable dedicated testnet account and dedicated signer.
2. Load signer secret only through the approved secret mechanism; never CLI arguments, source, commits, logs or evidence artifacts.
3. Hard-refuse mainnet at multiple boundaries.
4. Require an explicit one-shot test flag and exact deployment match.
5. Execute the minimum positive perpetual action.
6. Execute negative authorization tests for fund movement and withdrawal.
7. Revoke the signer.
8. Prove a new perpetual action fails after revocation.
9. Retain only sanitized evidence: public addresses, tx/order identifiers, timestamps, decoded public errors/reverts and commit/deployment identity.
10. Revoke/disable all disposable test credentials at completion or on any failure.

**Important:** creating this plan or harness does not authorize running signed tests.

## Phase E — Gate Result

The evaluator returns:

- `PASS` only if every required proof is explicit;
- `FAIL` if any unsafe capability is proven;
- `UNKNOWN` for any missing/ambiguous evidence.

Even `PASS` must not automatically enable RISEx writes. Write enablement remains a separate reviewed change and testnet/mainnet approvals remain separate gates.

## Rollback / Abort

At any sign of unexpected authorization scope, provider inconsistency or ambiguous deployment identity:

1. abort signed testing;
2. keep `RISExAdapter.writes_enabled = False`;
3. revoke any disposable signer if one was created under later explicit authorization;
4. preserve sanitized evidence;
5. classify the gate as `FAIL` or `UNKNOWN`, never optimistic `PASS`.

## Deliverables Before Signed-Test Authorization

- exact testnet deployment identity report;
- read-only contract/source/ABI evidence or explicit `UNKNOWN` gaps;
- authorization-scope analysis;
- unit/static tests for JWT/OperatorHub isolation;
- sanitized Phase 4 evidence schema;
- no runtime write enablement and no provider-side mutation.
