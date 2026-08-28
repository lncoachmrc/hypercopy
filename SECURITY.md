# Security

## Key custody

HyperCopy never requires or stores the user's primary wallet private key. It accepts only a Hyperliquid API/agent private key whose derived address is verified against the account's approved agents. The plaintext exists only for onboarding encryption and execution signing; logs redact key-like material.

Credentials use envelope encryption. A random 256-bit DEK encrypts the agent key with AES-256-GCM and record-bound AAD (`user_id + trading_account_id`). The DEK is wrapped separately. In local/staging, `KEK_PROVIDER=env` supports a 32-byte development KEK. Production mainnet refuses that configuration and requires `KEK_PROVIDER=aws_kms` with `ENCRYPTION_KEY_REFERENCE`.

For least privilege, use **different external KMS IAM principals** per Railway service: `api` may call `kms:GenerateDataKey` to encrypt onboarding credentials but must not have `kms:Decrypt`; `execution-worker` needs `kms:Decrypt` and should not be publicly reachable. `master-watcher` and `frontend` need no KMS credentials.

## Session/authentication

Wallet challenges are one-time, short lived, persisted in PostgreSQL for auditability and atomically consumed from Redis. Session JWTs are stored only in `HttpOnly; Secure; SameSite=Lax` cookies in production. Roles are re-read from PostgreSQL rather than trusted from a client token hint. Mutations require CSRF token + custom header.

Use production frontend/API custom domains under the same registrable domain (for example `app.example.com` and `api.example.com`) so SameSite=Lax remains compatible with credentialed API requests.

## Mainnet fail-closed gates

All must be true: environment network is `mainnet`; Railway variable `ENABLE_LIVE_TRADING=true`; PostgreSQL flag `live_trading` is enabled by an authenticated SUPERADMIN confirmation. Presence of a key alone can never activate mainnet.

## Secrets

Never commit database URLs/passwords, Redis credentials, Stripe secrets, session secrets, API wallet keys, KMS credentials or encryption KEKs. Production runtime secrets belong in Railway Variables; GitHub Actions has no production deploy responsibility and therefore should receive no application production secrets.

The authoritative repository secret-scanning gate is the GitHub Actions `secrets` job. It checks out complete history and runs the pinned official Gitleaks container with `gitleaks git` and no event-range `--log-opts`, so Gitleaks performs its full-history/all-refs git scan. `scripts/targeted_release_preflight.py` is only a targeted release-structure check with a narrow heuristic for obvious assignment mistakes; it must not be treated as a substitute for Gitleaks or another general-purpose secret scanner.

## Audit

`audit_logs` has a PostgreSQL trigger that rejects UPDATE/DELETE, making the application-level audit stream append-only. Actions carry actor, subject, reason, before/after and correlation ID where applicable. Do not log raw wallet signatures, session tokens, KMS material, Stripe secrets or decrypted credentials.

## Response to suspected agent-key compromise

Activate emergency stop for new exposure, rotate the wrapping KEK if relevant, and require the affected user(s) to revoke the named Hyperliquid agent at the exchange. Rotating encryption alone cannot invalidate a private key already exfiltrated.
