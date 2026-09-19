# TRAXION RISEx Negative Replay Probe Design

**Date:** 2026-09-19  
**Status:** approved for RED implementation planning; live execution NOT authorized  
**Baseline:** `main@20b87c58646bf785da773a2d04b13d6b6c47f074`

## Goal

Introduce a dedicated RISEx **testnet-only negative replay probe** whose only purpose is to determine whether RISEx rejects a second submission of the **exact same signed place-order request** after the first submission has consumed its nonce.

This implements the missing behavioral replay-rejection evidence required by ADR-0001 without weakening or modifying the ordinary signed-order path.

The existing manual probe remains one-shot and unchanged.

## Security statement

This tool deliberately submits the same signed place-order payload twice when, and only when, the first submission has been accepted and its nonce has subsequently been observed as consumed.

It exists exclusively for RISEx testnet, a disposable account, a dedicated revocable signer and provider minimum order size.

It must never be reused for production, RISEx mainnet, Hyperliquid, normal CopyJob execution, retries, benchmarking or arbitrary signed-request replay.

This warning must appear in the script module docstring and remain explicit in this specification.

## Deliberate gate exception — isolated negative-test exception only

The ordinary TRAXION RISEx path requires the replay-protection architecture collector to attest that the selected nonce is currently unused before an order is submitted. That fail-closed rule remains unchanged.

This negative probe contains one deliberate and narrowly scoped exception:

- `collect_replay_protection_architecture_attestation()` runs once before the first POST while the nonce is still unused;
- after the first POST succeeds and the same nonce is observed read-only as consumed, the collector is deliberately **not executed again** before the second submission;
- the second submission is permitted only because the purpose of this tool is to test whether RISEx itself rejects the already-consumed nonce when presented with the exact same previously attested signed payload.

This is the only approved place in TRAXION where a gate that would normally block the operation is deliberately not reapplied after state changes.

The exception:

- exists only inside this dedicated testnet negative-probe tool;
- does not weaken, modify or parameterize the ordinary collector;
- does not create an override flag on the ordinary execution path;
- does not apply to the manual order probe;
- does not apply to CopyJob execution;
- does not apply to continuous-window execution;
- does not apply to any mainnet path;
- is not a precedent for bypassing or omitting any other gate.

Future code or design work must not cite this probe as justification for bypassing a safety gate elsewhere. Any other exception requires its own explicit security review and ADR/spec decision.

## Separate entry point

Create:

`scripts/risex_negative_replay_probe.py`

The existing:

`scripts/risex_manual_order_probe.py`

must remain unchanged.

The new probe must not call, modify or weaken `_claim_one_shot_execution()` from the manual probe. It has its own immutable two-submission protocol.

## Explicit authorization

The probe requires a dedicated approval flag:

`--approve-two-identical-signed-order-submissions`

`--approve-testnet-order` does not authorize this probe.

The new approval flag states exactly what the operator is authorizing: up to two provider POST attempts containing the same signed order payload.

The following existing assertions remain mandatory:

- disposable account asserted;
- dedicated signer asserted;
- OperatorHub/JWT bypass disabled;
- ADR-0002 fund-movement-path absence asserted;
- `RISEX_SIGNED_WRITES_ENABLED == "true"` exactly;
- pinned RISEx testnet deployment PASS;
- signer/session valid;
- testnet only.

There is no production or mainnet override.

## Network restrictions

The tool supports only:

- RISEx testnet API;
- RISE chain ID `11155931`;
- pinned ADR-0005 deployment;
- existing testnet Authorization and Router identities.

There is no `--network` option.

There is no option capable of selecting RISEx mainnet.

Unexpected chain ID, deployment drift, account mismatch, signer mismatch or malformed runtime evidence fails closed before any POST.

## Order restrictions

The order is constructed through the existing typed RISEx preparation path.

Required properties:

- provider minimum order size;
- IOC;
- existing controlled manual-probe market intent;
- no caller-configurable size increase;
- no caller-configurable number of submissions;
- no retry loop;
- one generated `client_order_id`, reused unchanged;
- one action hash, reused unchanged;
- one nonce, reused unchanged;
- one deadline, reused unchanged;
- one signature, reused unchanged.

The request is prepared **once**.

`prepare_risex_ioc_request()` must never be called again after the first POST.

## Deadline

Use a fixed replay-probe deadline window of **60 seconds** from the observed chain block timestamp, bounded as usual by `session_expiration - 1`.

The deadline is not configurable from the CLI.

Before the second POST, obtain a fresh read-only block/session observation and require at least **15 seconds** remaining before the signed permit deadline.

If fewer than 15 seconds remain:

- do not regenerate the permit;
- do not extend the deadline;
- do not issue the second POST;
- report `DEADLINE_WINDOW_LOST`;
- terminate.

A timing failure therefore makes the experiment inconclusive rather than weakening its isolation.

## Replay architecture collector

`collect_replay_protection_architecture_attestation()` executes **exactly once**, before the first POST.

At that point it must prove, as today:

- pinned deployment identity;
- expected VerifyWitness runtime/typehash;
- API/on-chain nonce-state agreement;
- permit nonce equals the provider-selected current nonce;
- `isNonceUsed(...) == false`;
- signature recovery;
- action-hash binding;
- valid deadline/session window.

It is deliberately **not rerun after the first successful POST**, solely under the isolated exception defined above.

Rerunning it would correctly reject the now-consumed nonce and would prevent the behavioral experiment.

The ordinary collector remains unchanged and must continue to fail closed on consumed nonces.

## Request immutability

The probe retains the original `RISExPreparedPlaceOrderRequest` in process memory.

Before each provider submission, the existing signed transport may run its normal typed-request, identity, gate and freshness validation.

The replay collector itself does not rerun.

The serialized payload prepared for the second submission must be compared against the first before the second network call.

The following must be identical:

- complete order fields;
- account;
- signer;
- signature;
- nonce anchor;
- nonce bitmap index;
- deadline;
- action hash;
- client order ID.

Compute a canonical JSON SHA-256 fingerprint for both prepared payloads.

If the fingerprints differ, abort before the second POST with `PAYLOAD_IDENTITY_MISMATCH`.

The full permit and signature must never be logged.

## Hard submission budget

The probe implements an internal, non-configurable provider submission budget:

`MAX_SUBMISSIONS = 2`

The budget is consumed immediately before initiating each HTTP POST, not after receiving its result.

No code path may perform a third POST.

No exception causes an automatic retry.

No HTTP retry adapter is allowed.

No CLI parameter can alter the limit.

Operationally the experiment contains two intended submissions, but may terminate after only one if the first result or subsequent read-only evidence is unsuitable.

## First submission

The first submission uses the normally prepared, nonce-free signed request.

If the first POST:

- is explicitly rejected;
- times out;
- disconnects after transmission;
- produces malformed or ambiguous provider output;
- cannot be established as accepted;

the experiment terminates immediately.

The second POST does not run.

A timeout or connection-loss after sending is considered **ambiguous**, because the server may have accepted the request even though the client did not receive the response.

There is no retry.

## Mandatory post-first read-only evidence

After an accepted first POST and before the replay attempt, collect fresh on-chain nonce evidence at one explicit block tag.

For the exact permit nonce, record:

- block number;
- block tag;
- `isNonceUsed(account, anchor, index)`;
- `getNonceState(account)` anchor;
- `getNonceState(account)` bitmap.

The two calls must use the same explicit block tag.

The required condition is:

`isNonceUsed(...) == true`

and the relevant bit must be consistent with the returned bitmap.

If the nonce is not observed as consumed, or `isNonceUsed` and the bitmap disagree:

- no second POST;
- terminate;
- classify the result as `NONCE_CONSUMPTION_INCONSISTENT`.

Read-only observations may be repeated a small bounded number of times only to allow chain propagation, with no write retry. Proposed maximum:

- 3 observations;
- no more than 6 seconds total.

Every observation records its own explicit block number.

## Second-submission preconditions

Immediately before the second POST all of the following must still hold:

- same process;
- same original typed request;
- same payload fingerprint;
- same signature;
- nonce already observed as consumed;
- pinned deployment unchanged;
- signer/session still active;
- account and signer unchanged;
- permit deadline still valid;
- at least 15 seconds deadline margin;
- no ambient JWT/API/cookie authentication;
- submission budget has exactly one remaining slot.

If any condition fails, the second POST does not run.

## Second submission

The second provider POST is executed exactly once using the identical signed request.

No signing occurs between the first and second submissions.

No nonce selection occurs between the first and second submissions.

No new `client_order_id` is generated.

No new deadline is generated.

No new permit is generated.

## Provider error attribution boundary

The current public RISEx documentation describes bitmap-based nonce replay protection, `isNonceUsed`, `getNonceState`, unique permit nonces and the `POST /v1/orders/place` flow.

The public place-order example handles provider failures through `error.response?.data?.message`; it does not currently document a stable RISEx API error code specifically meaning “permit nonce already used” or “replay rejected”.

A generic EVM transaction error such as `NONCE_EXPIRED` is not sufficient attribution because it refers to transaction-account nonce semantics, not the RISEx VerifyWitness permit nonce.

Therefore implementation must not assume a nonce-specific error code exists.

If a live negative probe later returns a structured provider code/message that is unambiguously attributable to the consumed RISEx permit nonce or replay condition, that exact observed evidence may be used for `REPLAY_REJECTED_NONCE`.

If the provider rejects the second submission but the reason cannot be attributed specifically to nonce/replay, the result must remain `REPLAY_REJECTED_UNSPECIFIED`.

## Result classification

### `REPLAY_REJECTED_NONCE`

This is strong behavioral replay-rejection evidence only when:

- first submission was accepted;
- nonce consumption was confirmed read-only;
- payload identity was confirmed;
- deadline/session/deployment remained valid immediately before replay;
- the second request actually reached the RISEx provider;
- RISEx explicitly rejected that second request;
- the rejection contains structured or otherwise unambiguous provider evidence attributable specifically to the consumed permit nonce or replay condition.

Only this result may justify:

`behavioral_replay_rejection_proven = true`

after separate review of the captured sanitized evidence.

### `REPLAY_REJECTED_UNSPECIFIED`

Use this when:

- first submission was accepted;
- nonce consumption was confirmed;
- payload identity and deadline/session/deployment checks passed;
- the second request reached RISEx;
- RISEx explicitly rejected it;
- but the provider's response does not establish that nonce/replay was the reason.

This proves provider rejection of the replay attempt, but does **not** prove the rejection was caused by replay protection.

This result must leave:

`behavioral_replay_rejection_proven = false`

### `SECOND_SUBMISSION_AMBIGUOUS`

Use this status for:

- timeout after second transmission;
- connection loss;
- server-side 5xx;
- malformed response;
- any outcome for which provider rejection cannot be distinguished confidently from infrastructure failure.

No retry is permitted.

### `REPLAY_ACCEPTED_SECURITY_FAILURE`

If the second identical submission is accepted and returns evidence of another order/transaction:

- stop immediately;
- do not issue another write;
- mark replay protection as failed;
- retain sanitized order/transaction identifiers;
- retain both payload fingerprints;
- retain the nonce/deadline/action-hash metadata;
- perform read-only follow-up only;
- classify this as a provider security finding for RISEx.

The probe must not automatically close any resulting position, revoke credentials or perform any compensating write. Those are separate explicitly authorized operations.

## Output sanitation

Output follows the security principles of the manual probe.

Never output:

- private key;
- seed phrase;
- raw signature;
- full permit object;
- cookies;
- auth headers;
- environment secrets.

Allowed diagnostic fields include:

- network;
- account address;
- signer address;
- deployment fingerprint;
- block numbers;
- nonce anchor/index;
- bitmap;
- deadline;
- remaining deadline margin;
- action hash;
- client order ID;
- SHA-256 payload fingerprint;
- sanitized HTTP status/error;
- order ID;
- `sc_order_id`;
- tx hash;
- submission count;
- final result classification.

All diagnostic output is sanitized recursively before printing.

## Evidence persistence

The probe itself does not commit, push or modify the repository.

It emits one sanitized machine-readable final result.

After execution, evidence may be incorporated in a separate reviewed documentation change to:

`docs/security/risex-testnet-runtime-evidence-2026-09-10.md`

Only `REPLAY_REJECTED_NONCE`, backed by reviewed provider evidence attributable to nonce/replay, may justify documenting:

`behavioral_replay_rejection_proven = true`

`REPLAY_REJECTED_UNSPECIFIED` must not change that fact to true.

No code path synthesizes either conclusion.

## Implementation scope

New files only, unless RED demonstrates an unavoidable interface defect:

- `scripts/risex_negative_replay_probe.py`
- `backend/app/security/risex_consumed_nonce_evidence.py`
- unit tests for the script and the read-only evidence collector.

This specification itself is introduced separately in a documentation-only PR before RED.

Existing files that must remain unchanged during implementation unless a separately reviewed design change is approved:

- `scripts/risex_manual_order_probe.py`
- `backend/app/security/risex_replay_protection_architecture.py`
- `backend/app/security/risex_pre_order_gate.py`
- `backend/app/adapters/risex_signed_testnet_http.py`
- `backend/app/services/risex_signed_execution.py`
- execution worker routing;
- CopyJob routing;
- Hyperliquid execution;
- Railway configuration;
- database schema/migrations.

If implementation appears to require weakening one of those boundaries, stop and redesign.

## RED acceptance tests

RED must be committed separately before implementation and prove at minimum:

1. dedicated approval flag absent → zero POST;
2. `--approve-testnet-order` alone cannot authorize this tool;
3. non-testnet runtime → zero POST;
4. request is prepared exactly once;
5. replay architecture collector runs exactly once;
6. order uses provider minimum size;
7. deadline is fixed/non-configurable;
8. hard write budget never exceeds two submissions;
9. first explicit failure → exactly one POST;
10. first ambiguous transport outcome → exactly one POST;
11. first accepted + nonce not consumed → exactly one POST;
12. inconsistent `isNonceUsed`/bitmap → exactly one POST;
13. insufficient deadline margin → exactly one POST;
14. second preparation produces a different payload → exactly one POST;
15. valid path sends exactly two provider requests;
16. both submitted payloads are identical;
17. signature, nonce, deadline, action hash and client order ID are identical;
18. explicit nonce/replay-attributable second provider rejection → `REPLAY_REJECTED_NONCE`;
19. explicit but unattributed second provider rejection → `REPLAY_REJECTED_UNSPECIFIED`;
20. only `REPLAY_REJECTED_NONCE` can produce evidence eligible for `behavioral_replay_rejection_proven = true`;
21. second 5xx/timeout → `SECOND_SUBMISSION_AMBIGUOUS`, no retry;
22. second accepted submission → `REPLAY_ACCEPTED_SECURITY_FAILURE`;
23. second acceptance never causes a third POST;
24. output contains no permit or signature;
25. output contains no private key/environment secret;
26. existing manual probe remains one-shot;
27. `backend/tests/unit/test_risex_replay_protection_architecture_red.py::test_collector_fails_if_selected_nonce_is_already_used` remains green **without modification**;
28. existing RISEx security/transport tests remain green.

Test 27 is a mandatory regression invariant. This PR creates a dedicated negative-test tool that deliberately does not reapply the used-nonce collector after the first POST; the ordinary path must still reject an already-used selected nonce exactly as before.

## Stop conditions during implementation

Stop before GREEN if:

- a pre-existing test must be weakened;
- `test_collector_fails_if_selected_nonce_is_already_used` would need modification;
- the manual probe would have to become multi-shot;
- the replay architecture collector would have to accept an already-used nonce;
- the ordinary signed execution path would have to bypass nonce checks;
- the existing signed transport would have to allow mainnet;
- any retry is required to make the test work;
- any third write could occur;
- payload identity cannot be mechanically demonstrated;
- provider rejection cannot be distinguished sufficiently from network failure;
- an unattributed 4xx would have to be treated as nonce-specific behavioral proof.

## Definition of success

The implementation is ready for a separately authorized live negative probe only when:

- ordinary signed-order behavior is unchanged;
- manual probe remains one-shot;
- collector still rejects used nonces;
- the explicit regression test for consumed-nonce rejection remains green without modification;
- the deliberate collector non-reapplication is isolated to this tool and documented as a non-precedential exception;
- new tool is testnet-only;
- two-write ceiling is mechanically enforced;
- first failure prevents replay;
- payload identity is mechanically proven;
- nonce consumption is read-only confirmed before replay;
- deadline remains valid before replay;
- nonce-attributable rejection and unspecified rejection are distinct outcomes;
- only nonce-attributable rejection is eligible to prove behavioral replay rejection;
- second acceptance is treated as a security failure;
- there is no retry or third write;
- all required CI/security checks are green.

Implementation completion does **not** authorize execution.

Live execution remains a separate explicit operator decision.
