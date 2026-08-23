# Security

## Key custody

HyperCopy never requires or stores the user's primary wallet private key. It accepts only a Hyperliquid API/agent private key whose derived address is verified against the account's approved agents. The plaintext exists only for onboarding encryption and execution signing; logs redact key-like material.

Credentials use envelope encryption. A random 256-bit DEK encrypts the agent key with AES-256-GCM and record-bound AAD (`user_id + trading_account_id`). The DEK is wrapped separately. In local/staging, `KEK_PROVIDER=env` supports a 32-byte development KEK. Production mainnet refuses that configuration and requires `KEK_PROVIDER=aws_kms` with `ENCRYPTION_KEY_REFERENCE`.

For least privilege, use **different external KMS IAM principals** per Railway service: `api` may call `kms:GenerateDataKey` to encrypt onboarding credentials but must not have `kms:Decrypt`; `execution-worker` needs `kms:Decrypt` and should not be publicly reachable. `master-watcher` and `frontend` need no KMS credentials.

## Session/authentication

Wallet challenges are one-time, short lived, persisted in PostgreSQL for auditability and atomically consumed from Redis. A successful wallet signature creates two separate browser credentials:

- a 60-minute access JWT stored only in an `HttpOnly; Secure; SameSite=Lax` cookie in production;
- an opaque, one-time rotating refresh credential stored in a second `HttpOnly; Secure; SameSite=Lax` cookie with an absolute 24-hour lifetime from the original wallet signature.

Redis stores only an HMAC digest of the refresh credential, never the plaintext cookie value. Every successful refresh consumes the current Redis entry and creates a new opaque credential without extending the original 24-hour absolute expiration. After that boundary, after logout/revocation, or when the refresh credential is invalid, a new wallet signature is required.

The refresh endpoint does not depend on an expired access JWT. It requires the HttpOnly refresh cookie and the custom `X-Requested-With: HyperCopy` header; SameSite=Lax plus the non-simple custom header prevents a cross-site form from silently rotating the session. Authenticated mutations continue to require the access-session CSRF token plus the same custom header.

Roles are re-read from PostgreSQL rather than trusted from a client token hint. Refresh issuance also reloads the user from PostgreSQL before minting the replacement access JWT. The frontend retries an expired access request only after a successful refresh; if the 24-hour refresh window is no longer valid it clears local auth state and returns to login instead of leaving a stale authenticated shell visible.

IP addresses are hashed for audit/rate-limiting signals but are deliberately not a hard session-binding key: mobile networks, CGNAT, VPNs and normal ISP renewals can legitimately change an IP during the same trusted browser session.

Use production frontend/API custom domains under the same registrable domain (for example `app.example.com` and `api.example.com`) so SameSite=Lax remains compatible with credentialed API requests.

## Mainnet fail-closed gates

All must be true: environment network is `mainnet`; Railway variable `ENABLE_LIVE_TRADING=true`; PostgreSQL flag `live_trading` is enabled by an authenticated SUPERADMIN confirmation. Presence of a key alone can never activate mainnet.

## Secrets

Never commit database URLs/passwords, Redis credentials, Stripe secrets, session secrets, API wallet keys, KMS credentials or encryption KEKs. Production runtime secrets belong in Railway Variables; GitHub Actions has no production deploy responsibility and therefore should receive no application production secrets.

## Audit

`audit_logs` has a PostgreSQL trigger that rejects UPDATE/DELETE, making the application-level audit stream append-only. Actions carry actor, subject, reason, before/after and correlation ID where applicable. Do not log raw wallet signatures, session tokens, refresh credentials, KMS material, Stripe secrets or decrypted credentials.

## Response to suspected agent-key compromise

Activate emergency stop for new exposure, rotate the wrapping KEK if relevant, and require the affected user(s) to revoke the named Hyperliquid agent at the exchange. Rotating encryption alone cannot invalidate a private key already exfiltrated.
