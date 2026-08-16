# HyperCopy — Roadmap P0–P3

## P0 — Repository e infrastruttura

- [x] GitHub monorepo progettato.
- [x] Branch strategy `feature/* → PR → main` documentata.
- [x] GitHub Actions CI, CodeQL, Dependabot, Gitleaks configurati.
- [x] Docker Compose locale.
- [x] Railway Config-as-Code per frontend/API/watcher/worker.
- [x] Railway PostgreSQL come source of truth progettato.
- [x] Railway Redis come layer transiente progettato.
- [x] Private networking/reference variables documentate.
- [x] Catalogo environment variables e secrets.
- [x] Alembic versionato e Pre-Deploy solo API.
- [x] `/health/live` + `/health/ready`.
- [ ] Creare e validare il Railway Project reale.
- [ ] CI verde nel repository GitHub reale.

**Exit P0:** `main` buildabile/deployabile in staging, schema migrato, healthcheck verdi, zero secret nel repo.

## P1 — Shadow/Testnet

- [x] Hyperliquid adapter con SDK ufficiale e Cloid.
- [x] Hybrid position targeting + sizing puro.
- [x] Watcher con lease/fencing e replay.
- [x] Persistenza master event + fanout job durevole.
- [x] Weighted shared rate limiter Redis.
- [x] Risk Engine con asimmetrie apertura/chiusura.
- [x] Execution intent persistito prima dell'effetto esterno.
- [x] Reconciliation service e ledger persistente.
- [x] Shadow mode applicativo.
- [ ] Validazione completa adapter su Hyperliquid testnet reale.
- [ ] 20 casi trading E2E su testnet.
- [ ] Chaos test Redis/PG/worker/watcher/redeploy.
- [ ] Misurazione del rate budget dall'egress Railway reale.

**Exit P1:** testnet corretto, restart-safe dimostrato, zero duplicati nei chaos test.

## P2 — SaaS

- [x] Frontend React/TypeScript.
- [x] SIWE wallet auth + cookie HttpOnly + CSRF.
- [x] Onboarding agent Hyperliquid verificato.
- [x] Stripe Checkout/Portal/webhook idempotente.
- [x] Trial/basic/pro/enterprise.
- [x] Dashboard target/current/delta, executions, metriche.
- [x] Admin control room e audit.
- [x] Pause/Resume/Close positions separati.
- [x] Credential expiry monitoring.
- [ ] E2E browser reale staging.
- [ ] Stripe test-mode webhook/renewal/cancel/payment-failure E2E.
- [ ] UX/accessibility/security review.

**Exit P2:** flusso completo utente/admin su staging+testnet.

## P3 — Production hardening

- [ ] Mainnet **shadow mode ≥ 1 settimana**.
- [ ] KMS production least-privilege verificato.
- [ ] Railway PITR abilitato.
- [ ] Backup/restore drill completato.
- [ ] Runtime alerting configurato (non solo deployment healthcheck).
- [ ] Security audit esterno.
- [ ] Load test e failure injection.
- [ ] Railway redeploy sotto carico senza duplicati.
- [ ] Rollback code e migration strategy provati.
- [ ] Rate-limit capacity reale misurata.
- [ ] Canarino mainnet con size/utenti ridotti.
- [ ] Review legale/regolamentare applicabile prima dell'offerta retail.

**Exit P3:** tutte le caselle operative in `DEFINITION_OF_DONE.md` verificate con evidenza.
