# Temporary frontend audit exception: GHSA-mrhx-c7xc-m3w5

## Authorization and scope

- Decision owner: Luciano / GitHub `lncoachmrc`.
- Explicit approval: 9 November 2026, following the proposal for one time-limited exception in a separate PR, with **no merge or deployment**.
- Applies only to the frontend graph reviewed at PR #300 head `808ee33e9947f0f9cb9e3e6521ccd42fad3d20e4`.
- The new PR is stacked on `codex/frontend-reown-security-20261109`. It does not modify PR #300, PR #297, PR #298, PR #299 or main.
- Sole advisory: `GHSA-mrhx-c7xc-m3w5`, alias `CVE-2023-54404`. No generic exception for Zod, Reown, Coinbase, HIGH severity, or future advisories.
- Expires **9 December 2026 at 00:00 Europe/Rome**, i.e. `2026-12-08T23:00:00.000Z`, exclusive. The gate fails on every execution at or after that instant, even if the advisory has disappeared, until this temporary mechanism is explicitly removed or re-reviewed. It does not stop existing deployments or constitute a scheduled production action.
- There is no environment variable or command-line bypass for the deadline or scope.

## Primary evidence and residual uncertainty

Checked 9 November 2026:

1. The CVE authority record is **REJECTED**, dated 28 October 2026. The reason describes input-proportional memory use, dependent on missing application input-size limits, as expected validator behavior rather than a Zod defect.
   Source: https://github.com/CVEProject/cvelistV5/blob/main/cves/2023/54xxx/CVE-2023-54404.json
   Reviewed Git blob: `ba36617c3e5791cfd1290aca017284e9c82471fd`.
2. GitHub Advisory Database continues to list the advisory as active, HIGH, affecting `<4.6.6`, with an earlier update of 23 October 2026.
   Source: https://github.com/advisories/GHSA-mrhx-c7xc-m3w5
   Structured source: https://github.com/github/advisory-database/blob/main/advisories/github-reviewed/2026/10/GHSA-mrhx-c7xc-m3w5/GHSA-mrhx-c7xc-m3w5.json

This is a documented disagreement between registries, **not proof of universal safety** and not a declaration that oversized inputs are harmless. Reown/Coinbase still depend on Zod 3. Forcing Zod 4 or migrating the wallet layer solely to silence this finding is outside this authorization.

## Enforced restrictions

`frontend/scripts/audit-zod-exception.mjs` runs a fresh full `npm audit --json`, including development, optional and peer dependencies, against the public npm registry. It accepts only a valid version-2 report whose findings all terminate at the exact approved Zod advisory. Any other advisory, including LOW or INFO, blocks. Audit service errors, timeouts, signals, abnormal exit codes, malformed/truncated reports, count inconsistencies and unresolved/cyclic-only dependency references block.

The gate verifies byte-for-byte Git blob identities for:

| Input | Approved Git blob |
| --- | --- |
| `frontend/package.json` | `cbd042be13df238c76988b47abde0ed780b8e653` |
| `frontend/package-lock.json` | `61db0c5d4fa1a085fa24098d1d3cbd98acc53ef6` |
| `frontend/src/reown.ts` | `f7c80f026fd9176f70517eca5bf8e179e843bd36` |

The complete lock binding preserves the reviewed dependency edges and integrity hashes, not merely version ranges. Approved affected paths are enumerated in the script: the two Zod copies (3.25.76 and nested 3.22.4), the Reown 1.8.24 packages, Coinbase CDP 1.61.0 and abitype 1.0.6. Unknown paths or versions cannot inherit the exception. These hashes detect drift; package integrity remains checked by `npm ci`.

`email`, `socials`, `enableEmbedded` and `enableBaseAccount` must remain literal `false` in the reviewed Reown configuration. A complete source binding additionally rejects duplicate-property, spread or comment tricks, and deliberately requires review even after a harmless source refactor. This static guard **does not establish runtime unreachability**.

## Evidence, not concealed findings

The original npm report is never edited or filtered. Each gate run writes:

- `frontend/audit-evidence/npm-audit.raw.json`: original stdout, including every finding;
- `npm-audit.stderr.txt`: original stderr;
- `npm-audit.process.json`: command, raw exit code, signal and transport error;
- `gate-decision.json`: timestamp, exact advisory, expiry, raw finding count, exception use or failure reason.

CI uploads this directory with `if: always()`, a 30-day retention and failure when evidence is missing. A successful exception run is labeled `PASS_WITH_APPROVED_EXCEPTION`, not 'no vulnerabilities'. The remaining backend and landing audit policies are unchanged.

Commands from `frontend/`:

```sh
node --test scripts/audit-zod-exception.test.mjs
node scripts/audit-zod-exception.mjs
```

The script and tests use Node built-ins only. No runtime dependencies, lockfile overrides or application/trading logic are introduced.

## Mandatory pre-merge wallet verification: NOT EXECUTED

The audit exception authorizes no release. A successful audit gate cannot replace these tests. Keep this PR and the wallet dependency change draft until evidence is reviewed.

| Platform | Connection, signature, backend session, logout, reconnection | Runtime modules/network inspection |
| --- | --- | --- |
| Safari iPhone + MetaMask | PENDING | PENDING |
| Safari iPhone + Rabby | PENDING | PENDING |
| Android | PENDING | PENDING |
| Desktop | PENDING | PENDING |

Use an isolated staging environment and a dedicated test wallet; no main-wallet secret, funded wallet or trading order is needed. Record tested commit, browser/wallet versions and sanitized results. Inspect the runtime network/module loading for the Reown embedded-auth iframe and Base Account SDK; configuration alone does not prove they are absent. Any unexpected activation/loading requires investigation before merge.

Also require integrated CI after combining #297/#298/#299/#300 and this exception on an isolated integration branch, genuine staging validation and explicit merge/production authorization. Do not use `risex-e2e` as AI staging.

## Removal and review procedure

Before expiry the decision owner must recheck CVE/GHSA and available upstream fixes. If the advisory is withdrawn from npm or a compatible upstream fix is adopted, remove this temporary gate and restore the normal frontend audit command after verification. Do not silently extend the deadline or refresh the reviewed hashes. Any changed graph/configuration requires a fresh risk review and authorization. No automatic approval or renewal is provided.
