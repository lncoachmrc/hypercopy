# RISEx Signer Security Evidence Gate

**Date:** 2026-09-09  
**Project:** TRAXION  
**Scope:** RISEx follower execution authorization  
**Current gate state:** `BLOCKED / UNKNOWN`

## Purpose

TRAXION may store and use only a dedicated, revocable execution credential that is demonstrably incapable of moving user funds. For RISEx, a signer is acceptable only if evidence proves that it is limited to perpetual-trading actions and cannot transfer, withdraw or otherwise move funds.

This document defines the evidence required to move the RISEx write gate from `UNKNOWN` to `PASS`. A `PASS` report is evidence only: it does **not** enable RISEx writes, change `ENABLE_LIVE_TRADING`, change the database `live_trading` flag, change a user's copy state, deploy code, or authorize mainnet.

## Current Evidence Limitation

As of 2026-09-09, the public RISEx signer registration reference documents account, signer, expiration and signature/nonce material but does not expose a documented granular permission field that proves `PERPS`-only authorization. Public session-status/signer-list reads can provide operational evidence, but API-provided permission labels are not accepted by TRAXION as independent on-chain least-privilege proof.

RISEx order documentation also exposes a JWT/OperatorHub authorization route in addition to signer/permit flows. Therefore the TRAXION trading worker must be proven unable to use any authorization path that bypasses the restricted signer.

Relevant public references:

- `https://developer.rise.trade/reference/authservice_registersigner`
- `https://developer.rise.trade/reference/orderservice_placeorder`
- `https://developer.rise.trade/reference/general-information`

## Verdict Semantics

### PASS

`PASS` is allowed only when every required check is explicitly proven:

1. **Deployment identity**
   - chain ID is known;
   - the runtime Authorization/verifying contract is identified;
   - the runtime router is identified;
   - evidence corresponds to the exact deployment being tested.

2. **Session state**
   - signer is active;
   - signer is bound to the expected account;
   - signer is not expired.

3. **Least-privilege permission proof**
   - permission evidence is independently verified on-chain;
   - `PERPS` is present;
   - `ALL`, `MOVE_FUNDS`, `MOVE_FUND`, `MOVEFUND`, transfer, withdrawal and spot permissions are absent;
   - no additional unclassified permission remains.

4. **Controlled positive perpetual test**
   - the same signer can perform the intended perpetual execution action on testnet.

5. **Controlled negative fund-movement test**
   - a fund-movement attempt made with the same signer is rejected by RISEx authorization itself.

6. **Controlled negative withdrawal test**
   - a withdrawal attempt made with the same signer is rejected by RISEx authorization itself.

7. **Revocation test**
   - after signer revocation, a new perpetual action using that signer is rejected.

8. **Authorization-path isolation**
   - the TRAXION trading worker cannot use JWT/OperatorHub or another route to bypass the restricted signer.

### FAIL

Any proven unsafe condition makes the gate `FAIL`, including:

- signer inactive or revoked when expected active;
- signer bound to another account;
- signer expired;
- a forbidden permission such as fund movement, withdrawal, transfer, spot or `ALL` exists;
- the controlled perpetual action fails;
- fund movement or withdrawal is not rejected by RISEx authorization;
- an order still succeeds after revocation;
- an alternate JWT/OperatorHub bypass remains reachable from the trading worker.

### UNKNOWN

Missing or ambiguous evidence is `UNKNOWN`, never `PASS`. Examples:

- numeric signer status with no documented semantic mapping;
- permission labels returned only by an API and not independently proven on-chain;
- missing contract/deployment identity;
- behavioral positive/negative tests not yet run;
- inability to prove that the worker cannot use an alternate authorization path;
- provider read failure or malformed response.

`UNKNOWN` keeps the RISEx write gate blocked.

## Implemented Public Read-Only Probe

The current probe collects only public evidence with HTTP `GET` requests:

- `/v1/auth/eip712-domain`
- `/v1/system/config`
- `/v1/auth/session-key-status`
- `/v1/auth/signers`

The HTTP transport rejects every `POST` locally before network I/O. The RISEx adapter independently rejects all mutating methods while `writes_enabled` remains statically `False`.

Run from the repository root using only public addresses:

```bash
python scripts/risex_signer_probe.py \
  --network testnet \
  --account 0x1111111111111111111111111111111111111111 \
  --signer 0x2222222222222222222222222222222222222222
```

Optional public API override:

```bash
python scripts/risex_signer_probe.py \
  --network testnet \
  --account 0x1111111111111111111111111111111111111111 \
  --signer 0x2222222222222222222222222222222222222222 \
  --base-url https://api.testnet.rise.trade
```

The command intentionally has no private-key, seed, password, token or signature option.

Exit codes:

- `0`: `PASS`
- `1`: `FAIL`
- `2`: `UNKNOWN` or provider read unavailable

With public API evidence alone, the expected safe result is normally `UNKNOWN` because on-chain least-privilege proof and controlled behavioral tests are intentionally not supplied by the read-only collector.

## Evidence Report Rules

A retained security-evidence report may contain only public/non-secret material such as:

- network and chain ID;
- account address;
- signer public address;
- Authorization/verifying contract and router addresses;
- expiration/status evidence;
- normalized permission result;
- transaction hashes or decoded revert reasons from future controlled Phase 4 tests;
- probe verdict and per-check results;
- exact TRAXION commit and exact RISEx deployment tested.

It must never contain:

- seed phrase;
- main-wallet private key;
- signer private key;
- reusable signature material;
- session cookie/token;
- authorization bearer/JWT;
- other secrets.

## Separate Phase 4 Required Before Any Signed Test

The current foundation does not register a signer, persist a RISEx signer private key, place/cancel an order, update leverage, move funds or perform a withdrawal.

A separate Phase 4 plan/PR is required to perform controlled signed testnet verification. That plan must:

1. identify the exact RISEx testnet contracts and permission mechanism;
2. define a disposable dedicated test account/signer and rollback/revoke procedure;
3. prove `PERPS`-only authorization on-chain;
4. perform the positive perpetual test;
5. perform negative fund-movement and withdrawal tests;
6. revoke the signer and prove post-revoke rejection;
7. prove that JWT/OperatorHub cannot be used by the TRAXION trading worker;
8. retain only sanitized evidence;
9. require explicit authorization before executing any signed test.

Even a successful testnet `PASS` does not authorize mainnet. The complete evidence gate must be repeated against the exact RISEx mainnet deployment before any mainnet approval can be considered.
