# HyperCopy — Definition of Done

**Regola:** una casella che richiede Railway/Hyperliquid/Stripe reali resta non spuntata finché non esiste evidenza del test. Il fatto che il codice sia presente non equivale a produzione validata.

## Repository / GitHub

- [x] Monorepo GitHub riproducibile strutturato.
- [x] Nessun secret reale inserito intenzionalmente nel repository.
- [x] GitHub Actions CI definita.
- [x] CodeQL / dependency update / secret scan definiti.
- [x] `main` definita come branch deployabile.
- [x] Package lock frontend presente.
- [x] Alembic migrations versionate.
- [ ] GitHub Actions verdi sul repository remoto reale.
- [ ] Branch protection `main` verificata.

## Railway / infrastruttura

- [x] Railway Project topology documentata.
- [x] `frontend`, `api`, `master-watcher`, `execution-worker` Config-as-Code presenti.
- [x] PostgreSQL Railway progettato come source of truth.
- [x] Redis Railway progettato come transiente/ricostruibile.
- [x] Reference variables DB/Redis documentate.
- [x] Private networking documentato.
- [x] Migrazioni solo API via Pre-Deploy.
- [x] `/health/live` e `/health/ready` distinti.
- [x] Graceful shutdown watcher/worker implementato.
- [x] Watch paths distinti.
- [x] Overlap/draining Railway configurati.
- [ ] Railway Project reale creato e documentato.
- [ ] PostgreSQL Railway reale configurato.
- [ ] Redis Railway reale configurato.
- [ ] Private networking reale verificato.
- [ ] Frontend Railway funzionante.
- [ ] API Railway funzionante.
- [ ] Master Watcher Railway funzionante.
- [ ] Execution Worker Railway funzionante.
- [ ] Domains/HTTPS/WSS verificati.

## Hyperliquid correctness

- [x] SDK ufficiale usato con `LocalAccount`/`Info`/`Exchange`.
- [x] Cloid deterministico a 128 bit.
- [x] Intento persistente `SUBMITTING` prima dell'invio.
- [x] Nessun blind retry dopo risultato ambiguo.
- [x] `szDecimals`, `maxLeverage`, `onlyIsolated` letti da metadata.
- [x] Minimum notional preserva il delta.
- [x] IOC + `reduce_only` + `expiresAfter`.
- [x] Main wallet private key rifiutata.
- [x] Agent verificato contro `extraAgents` e scadenza monitorata.
- [ ] Ogni chiamata adapter verificata su testnet reale.
- [ ] Budget rate-limit misurato sull'egress Railway reale.
- [ ] 20 trading cases E2E completati.

## Trading correctness / convergence

- [x] Hybrid position targeting implementato.
- [x] Sizing per exposure ratio / eligible equity.
- [x] Delta rispetto al ledger follower.
- [x] Riduzioni reduce-only.
- [x] Inversione separata close→open.
- [x] Mark price persistente per asset nel ledger.
- [x] Free margin exchange-derived persistito nello snapshot.
- [x] Idempotenza persistente in PostgreSQL.
- [x] Reconciliation periodica implementata.
- [x] Reconciliation dopo restart/reconnect prevista.
- [x] Drift classificato e auditato.
- [x] Policy manual trade modellata.
- [x] Redis loss ricostruibile da PostgreSQL per design.
- [ ] Fill parziali assorbiti dimostrati su testnet reale.
- [ ] Restart watcher con due processi concorrenti testato su PostgreSQL reale.
- [ ] Restart worker durante ordine ambiguo testato.
- [ ] Perdita Redis totale dimostrata con chaos test.
- [ ] Railway redeploy sotto carico: zero duplicati.

## Risk Engine

- [x] User pause/global pause/emergency stop distinti.
- [x] Pause e Close positions sono operazioni distinte.
- [x] Entitlement scaduto blocca nuove esposizioni ma non riduzioni.
- [x] Drawdown/daily loss bloccano nuove esposizioni ma non riduzioni.
- [x] Max per trade con trimming.
- [x] Max exposure totale/per asset.
- [x] Max leverage/max positions/free margin.
- [x] Asset allow/blocklist.
- [x] Near-liquidation halt.
- [x] Stale-data fail closed.
- [x] Local unit test suite su sizing/risk/reconcile/state/crypto: verde nell'ambiente di costruzione.
- [ ] Risk integration test con stato Hyperliquid reale.

## Security

- [x] Nessuna main wallet key follower richiesta/stoccata.
- [x] Envelope AES-256-GCM + DEK per record.
- [x] AAD legata a user/account.
- [x] Provider KMS esterno implementato (`aws_kms`).
- [x] API/worker privilege separation documentata.
- [x] SIWE replay-safe con nonce monouso.
- [x] Sessione cookie HttpOnly; no token in localStorage.
- [x] CSRF protection.
- [x] CSP senza `unsafe-inline`.
- [x] Audit append-only DB trigger.
- [x] Structured logging/redaction.
- [x] Stripe webhook firma + idempotenza persistente.
- [ ] KMS IAM least privilege verificato in production.
- [ ] KEK rotation/rewrap drill completato.
- [ ] Security audit esterno completato.

## SaaS / UX

- [x] Connect wallet/login.
- [x] Dashboard equity/PnL/drawdown/Sharpe.
- [x] Target/current/delta visibili.
- [x] Execution history con reject reason.
- [x] Risk profile UI.
- [x] Billing UI.
- [x] Admin control room.
- [x] Shadow state support.
- [ ] Browser E2E reale completato.
- [ ] Accessibility audit completato.

## Backup / Recovery / Operations

- [x] `RUNBOOK.md` presente.
- [x] Redis recovery script presente.
- [x] Rollback procedure documentata.
- [x] PITR procedure documentata.
- [x] Runtime metrics implementate.
- [ ] Railway PITR abilitato.
- [ ] Database backup testato.
- [ ] Database restore testato.
- [ ] Rollback deployment testato.
- [ ] Monitoring/alerting esterno/runtime attivo.
- [ ] Runbook tabletop/incident drill completato.

## Mainnet

- [x] Testnet/mainnet separati da config.
- [x] Live trading richiede network mainnet + env flag + DB flag.
- [x] Production mainnet rifiuta KEK env.
- [ ] Mainnet shadow ≥ 1 settimana.
- [ ] Shadow report analizzato/approvato.
- [ ] Controlled mainnet canary completato.
- [ ] Review legale/regolamentare completata per giurisdizione/target utenti.

**Nessun trading con denaro reale finché i gate P1/P3 rilevanti restano aperti.**
