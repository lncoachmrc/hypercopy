# ADR-0001 — RISEx testnet verification on Railway production

- **Status:** Accepted
- **Date:** 2026-09-11
- **Scope:** TRAXION / RISEx signed testnet verification
- **Decision owner:** TRAXION project owner

## Context

TRAXION currently has no available capacity for a separate Railway staging environment suitable for the RISEx signed testnet verification sequence.

The previous prerequisite that required a dedicated staging environment is therefore no longer a **Must** prerequisite for this verification work. The verification still targets RISEx **testnet** and must not relax the existing testnet-only safety boundaries in code.

Using the Railway production environment for verification creates additional operational risk because production services, production data, queues and runtime configuration coexist with the test harness. The environment choice therefore requires explicit compensating controls before any provider-side test mutation is attempted.

## Decision

RISEx signed testnet verification will be executed from the **Railway production environment**, with the following mandatory mitigations and execution discipline:

1. A restorable production database backup must exist before verification starts, with timestamp and restore procedure recorded.
2. Verification must use a dedicated test user/account and must not use a real customer account.
3. Before provider-side verification begins, the order-execution path must be checked for queued jobs belonging to any other user. If any are present, verification stops.
4. The execution kill switch must be documented in advance, including the exact procedure to pause executions and stop already queued jobs. It must not be improvised after an incident.
5. Runtime credentials/configuration used by the RISEx verification path must be confirmed as **testnet**, not mainnet. Secret values must never be exposed in logs or documentation.
6. Negative gates must be executed one at a time and all must pass before the first positive order:
   - `perps_only_scope`: out-of-scope request rejected;
   - fund/withdraw attempt rejected;
   - replay of a signed request rejected.
7. Any unexpected result triggers immediate stop and kill-switch activation. No workaround or continuation is permitted in the same verification sequence.
8. The first positive verification order, when authorized by the preceding gates, must be a single minimum-size testnet order.
9. Fund and withdraw operations remain prohibited throughout the verification sequence.
10. Post-revoke verification must confirm that reuse of the revoked signer is rejected by the provider.
11. Each verification step is executed and reported separately. No unattended batch execution is permitted.

This decision changes only the **execution environment prerequisite**. It does not authorize RISEx mainnet trading, production-user trading changes, fund/withdraw operations, or relaxation of the existing signer, deployment, replay, epoch or ambient-authentication controls.

## Options considered and rejected

### Option A — Separate Railway staging environment

Rejected for the current verification cycle because the project has no available capacity for a separate environment. This remains the preferred isolation model if capacity becomes available later.

### Option B — Local or ad-hoc environment outside Railway

Rejected because it would not validate the same deployed service topology, runtime configuration, database/queue interactions and operational controls used by TRAXION on Railway.

### Option C — Production environment without additional controls

Rejected because sharing production runtime, queues and data without backup, dedicated identity, queue isolation, predeclared kill switch and explicit testnet credential verification would create unacceptable operational ambiguity.

## Consequences

### Positive

- RISEx testnet verification can proceed without waiting for additional Railway environment capacity.
- The verification exercises the actual deployed TRAXION service topology and production runtime characteristics.
- Operational controls are made explicit and auditable before provider-side mutations occur.

### Negative / risks

- Production services and data are present during verification, increasing blast radius if isolation assumptions fail.
- Queue state, runtime configuration and credential routing must be verified immediately before each relevant step.
- The procedure is intentionally slower because each gate is executed and reviewed separately.
- A production database backup and tested restore path become mandatory prerequisites.

## Conditions for review

This ADR must be reviewed when any of the following occurs:

- Railway capacity becomes available for a dedicated staging environment;
- the RISEx integration moves beyond controlled testnet verification;
- the execution architecture, queue model, credential model, destination-epoch model or kill-switch mechanism changes materially;
- RISEx changes its testnet/mainnet authentication, signer, replay or order-routing behavior;
- an incident or near-miss occurs during verification;
- production load or active-user execution makes safe queue isolation impractical;
- the project introduces a stronger isolated verification environment with equivalent production topology.

Until this ADR is superseded, **Railway production is the approved environment for RISEx testnet verification, subject to the mitigations above**.
