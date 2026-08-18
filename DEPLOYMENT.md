# HyperCopy — Deployment GitHub → Railway → Hyperliquid

Questa è la procedura operativa completa per portare il monorepo da `git clone` a un ambiente Railway funzionante. Non usare provider applicativi alternativi: il runtime è Railway, il codice è GitHub, l'exchange è Hyperliquid.

## 1. Prerequisiti

- Repository GitHub con branch protetta `main`.
- Railway Project (consigliato piano con risorse/backup adeguati alla produzione).
- Account Stripe e tre Price ID (`basic`, `pro`, `enterprise`).
- Wallet master Hyperliquid: **serve solo l'indirizzo pubblico**.
- Per ogni follower: account Hyperliquid + agent/API wallet nominato `hypercopy`.
- Per mainnet production: KMS esterno. Questa repo implementa `aws_kms`; è l'unica dipendenza infrastrutturale esterna prevista per la custodia avanzata della KEK.
- Progetto Reown AppKit per il collegamento dei wallet self-custodial. Il Project ID è pubblico e resta configurato per ambiente.

## 2. Branch / CI

Flusso obbligatorio:

```text
feature/* → Pull Request → GitHub Actions → review → merge main → Railway GitHub deploy
```

`main` deve essere sempre deployabile. GitHub Actions esegue test, Alembic su PostgreSQL CI, build frontend, dependency audit, CodeQL/gitleaks. GitHub Actions **non** esegue Railway CLI e non possiede segreti runtime di produzione.

In GitHub, proteggere `main` richiedendo i check `backend`, `frontend`, `secrets` (e CodeQL quando abilitato) prima del merge.

## 3. Railway Project

Creare un unico Project, poi un Environment `staging` e un Environment `production`. Per MVP è accettabile iniziare solo con staging; non attivare mainnet fino ai gate in `DEFINITION_OF_DONE.md`.

Aggiungere al Project due database Railway:

1. **Postgres** — persistent source of truth.
2. **Redis** — transiente; stream, rate limiter, nonce, pub/sub, cache.

Non generare TCP proxy/database domain pubblici per l'applicazione. I servizi applicativi usano le reference variables Railway sulla private network.

## 4. Servizi applicativi da creare

Creare **quattro** servizi dallo stesso repository GitHub.

| Service | Root Directory | Config File | Public domain | Start Command | Pre-deploy | Healthcheck | Repliche iniziali |
|---|---|---|---:|---|---|---|---:|
| `frontend` | `/frontend` | `/frontend/railway.toml` | sì | Docker ENTRYPOINT (Nginx) | — | `/` | 1 |
| `api` | `/backend` | `/backend/railway.toml` | sì | da config: Uvicorn | `alembic upgrade head` | `/health/ready` | 2 production / 1 staging |
| `master-watcher` | `/backend` | `/backend/railway.watcher.toml` | **no** | `python -m app.workers.watcher` | **mai** | nessun domain | 1 |
| `execution-worker` | `/backend` | `/backend/railway.worker.toml` | **no** | `python -m app.workers.execution_worker` | **mai** | nessun domain | 1 → N |

### Perché Root Directory + Config File sono entrambi necessari

Railway tratta questo repository come isolated monorepo. Impostare la Root Directory limita i file usati dal build; il Config File va indicato con percorso assoluto dal root Git (`/backend/...`, `/frontend/...`). I `watchPatterns` sono già versionati nei TOML e impediscono che una modifica solo frontend ridistribuisca i worker.

## 5. Build Command

Nessun Build Command custom è necessario: i quattro servizi usano `builder = "DOCKERFILE"`.

- backend: `/backend/Dockerfile`
- frontend: `/frontend/Dockerfile`

Il frontend esegue `npm ci` + `npm run build` nello stage Node e serve `dist/` con Nginx nello stage runtime.

## 6. Reference variables e private networking

Sui servizi `api`, `master-watcher`, `execution-worker` impostare:

```text
DATABASE_URL=${{Postgres.DATABASE_URL}}
REDIS_URL=${{Redis.REDIS_URL}}
```

Non usare `DATABASE_PUBLIC_URL`, host/porta/password hardcoded o credenziali nel repository.

Il browser usa sempre il dominio del frontend. Nginx inoltra `/api/v1` e WebSocket all'API via Railway Private Network, preservando cookie SameSite/HttpOnly e CSRF same-origin.

```text
Internet
  └─ frontend (Nginx)
       └─ Railway Private Network
          └─ api
              ├─ Postgres
              └─ Redis

master-watcher ──private──> Postgres/Redis ──outbound──> Hyperliquid
execution-worker ─private─> Postgres/Redis ──outbound──> Hyperliquid
```

L'API può mantenere un dominio pubblico per webhook/health operativi previsti dall'architettura, ma il browser TRAXION non deve usarlo per autenticazione o sessione.

## 7. Variabili comuni backend

Usare Railway Shared Variables solo per valori davvero identici e non privilegio-specifici. Base consigliata:

```text
APP_ENV=staging|production
APP_VERSION=${{RAILWAY_GIT_COMMIT_SHA}}
LOG_LEVEL=INFO
PUBLIC_APP_URL=https://app.example.com
API_BASE_URL=https://api.example.com

DATABASE_URL=${{Postgres.DATABASE_URL}}
REDIS_URL=${{Redis.REDIS_URL}}

HYPERLIQUID_NETWORK=testnet|mainnet
HYPERLIQUID_MASTER_ADDRESS=0x...
HL_RATE_BUDGET_PER_MIN=1200
HL_ORDER_EXPIRES_AFTER_MS=15000
HL_AGENT_NAME=hypercopy
ENABLE_LIVE_TRADING=false
DEFAULT_SHADOW_MODE=true

SESSION_SECRET=<Railway secret, 48+ random bytes/chars>
SIWE_DOMAIN=app.example.com
SIWE_URI=https://app.example.com

STRIPE_SECRET_KEY=<secret>
STRIPE_WEBHOOK_SECRET=<secret>
STRIPE_PRICE_BASIC=price_...
STRIPE_PRICE_PRO=price_...
STRIPE_PRICE_ENTERPRISE=price_...
TRIAL_DAYS=14

ADMIN_ADDRESSES=0x...,0x...
SUPERADMIN_ADDRESSES=0x...
RECONCILE_INTERVAL_SECONDS=60
LEDGER_STALE_SECONDS=120
WATCHER_LEASE_TTL_SECONDS=15
WATCHER_LEASE_RENEW_SECONDS=5
JOB_LEASE_SECONDS=120
MAX_JOB_RETRIES=5
METRICS_TOKEN=<random secret>
```

`SESSION_SECRET`, Stripe secret/webhook, KMS credentials e signing credentials non vanno in GitHub Actions.

## 8. KMS — variabili per servizio

### Local/staging non-mainnet (solo sviluppo controllato)

```text
KEK_PROVIDER=env
ENCRYPTION_KEY_B64=<32 byte base64>
```

`APP_ENV=production` + `HYPERLIQUID_NETWORK=mainnet` rifiuta per codice `KEK_PROVIDER=env`.

### Production mainnet

```text
KEK_PROVIDER=aws_kms
ENCRYPTION_KEY_REFERENCE=<KMS key ARN/id>
AWS_REGION=<region>
```

Separare i principal:

- `api`: permesso minimo `kms:GenerateDataKey` sulla key; **no `kms:Decrypt`**.
- `execution-worker`: `kms:Decrypt` sulla key; nessun dominio pubblico.
- `master-watcher`: nessun permesso KMS.
- `frontend`: nessun permesso KMS.

Le credenziali AWS necessarie al singolo service sono Railway Variables di quel service, non Shared Variables e non GitHub secrets.

## 9. Variabili frontend e Reown AppKit

Sul solo service Railway `frontend` impostare:

```text
REOWN_PROJECT_ID=<Project ID del progetto TRAXION in Reown Dashboard>
API_PROXY_UPSTREAM=http://api.railway.internal:8080
```

`REOWN_PROJECT_ID` è un identificatore frontend pubblico, non una chiave privata. Viene scritto a runtime in `/config.js`: può quindi essere diverso tra staging e production senza ricompilare il bundle. Se manca, la pagina login resta operativa e mostra un errore di configurazione controllato senza inizializzare AppKit.

`API_BASE_URL` e `WS_URL` restano disponibili per lo sviluppo locale; in staging/production il browser usa `/api/v1` same-origin e Nginx inoltra il traffico all'upstream privato.

### Reown Dashboard — checklist TRAXION

1. Creare un progetto **TRAXION** nel Reown Dashboard.
2. Copiare il Project ID e inserirlo come `REOWN_PROJECT_ID` esclusivamente nel service Railway `frontend` dell'ambiente corrispondente.
3. In **Allowed Origins**, aggiungere l'origin HTTPS reale di production, con protocollo e hostname esatti e senza path.
4. Aggiungere separatamente l'origin HTTPS reale di staging.
5. Non riutilizzare placeholder `example.com`: questa repository non contiene un dominio TRAXION reale verificabile.
6. Verificare che l'URL aperto nel browser, l'Allowed Origin e `metadata.url` coincidano esattamente per origin; il frontend imposta `metadata.url = window.location.origin`.
7. Ripetere la verifica per eventuali sottodomini distinti: ogni origin effettivamente usato deve essere autorizzato nel Dashboard Reown.

Reown viene usato solo per connessione wallet e firma EVM. Challenge, verifica firma, sessione HttpOnly, CSRF e logout restano nel backend TRAXION. Non configurare Reown Authentication, SIWE/SIWX, email/social login, embedded wallet o smart account.

### CSP AppKit

`frontend/nginx.conf` mantiene `script-src 'self'`, `frame-ancestors 'none'`, `object-src 'none'` e `base-uri 'self'` e aggiunge solo le origini necessarie al flusso wallet:

| Origine | Direttiva | Funzione |
|---|---|---|
| `rpc.walletconnect.com/.org` | `connect-src` | RPC EVM usato da AppKit |
| `relay.walletconnect.com/.org` | `connect-src` HTTPS/WSS | relay WalletConnect |
| `api.web3modal.com/.org` | `connect-src`, `img-src` | catalogo e asset wallet AppKit |
| `keys.walletconnect.com/.org` | `connect-src` | chiavi/Verify WalletConnect |
| `verify.walletconnect.com/.org`, `secure.walletconnect.com/.org` | `frame-src` | WalletConnect Verify |
| `fonts.reown.com` | `font-src` | font AppKit |
| `cca-lite.coinbase.com`, `wss://www.walletlink.org` | `connect-src` | connettore Coinbase EOA |

Analytics, notifiche, on-ramp e swap sono disabilitati e le relative origini non vengono autorizzate. In staging controllare la console CSP durante MetaMask, Rabby, WalletConnect e Coinbase; ogni nuova origine deve essere documentata qui con la sua funzione prima di entrare in production. Non aggiungere wildcard, `unsafe-eval` o `unsafe-inline` per risolvere errori CSP.

## 10. Domains

Assegnare domini pubblici solo a:

- `frontend`: dominio applicativo reale
- `api`: dominio API reale, se richiesto per webhook/health esterni

Usare la stessa registrable domain quando possibile. Il login browser resta comunque same-origin attraverso Nginx. Configurare Stripe webhook sull'endpoint API pubblico effettivo.

## 11. Migrazioni Alembic

**Unica autorità: `api`.** `/backend/railway.toml` contiene:

```toml
preDeployCommand = "alembic upgrade head"
```

Watcher/worker hanno schema guard ma non migrano. Se Alembic fallisce, il deploy API deve fallire e il nuovo codice non deve essere attivato.

Regola schema evolution:

1. migration additive/backward-compatible;
2. deploy codice che smette di usare il vecchio campo;
3. solo in un deploy successivo rimozione distruttiva;
4. backup/PITR prima di migration critica.

## 12. Healthcheck

API:

- `/health/live`: processo vivo, niente query pesanti.
- `/health/ready`: PostgreSQL + Redis + Alembic revision.

Railway healthcheck è configurato su `/health/ready`. È un deployment gate; il runtime monitoring deve usare metriche/alert separati.

Frontend usa `/` come healthcheck di deploy.

Watcher/worker non espongono una porta pubblica. La loro salute operativa è misurata da lease/heartbeat/metriche nell'admin control room.

## 13. Restart policy e graceful shutdown

Configurazione versionata:

- `api`: `ON_FAILURE`, overlap 20s, draining 30s.
- `master-watcher`: `ALWAYS`, 1 replica, overlap 20s, draining 30s.
- `execution-worker`: `ALWAYS`, overlap 20s, draining 60s.
- `frontend`: `ON_FAILURE`.

Durante OLD + NEW:

- API è stateless.
- watcher: solo lease PG valido + fencing token può scrivere.
- worker: consumer identity distinta + claim PG `FOR UPDATE SKIP LOCKED` + Cloid deterministico.
- SIGTERM watcher rilascia lease; il successore recupera/reconcilia.
- SIGTERM worker termina in sicurezza il lavoro corrente entro il draining o lascia il job recuperabile.

## 14. Scaling

### API

Production: 2 repliche sono ragionevoli per availability. È stateless; sessione/DB/Redis sono esterni.

### Master Watcher

**1 replica configurata**, ma la correctness deriva dal lease/fencing PG, non dal numero di repliche. Non scalare orizzontalmente per throughput.

### Execution Worker

Parte da 1. Scala a N solo dopo test del rate budget reale. Ogni replica usa `RAILWAY_REPLICA_ID`; job ownership è in PostgreSQL e Redis Stream è un acceleratore.

Non scalare pensando di ottenere automaticamente più throughput Hyperliquid: il rate limiter è condiviso proprio perché il budget outbound va governato globalmente.

## 15. Staging

Consigliato prima della produzione:

- Railway Environment `staging`
- Postgres/Redis separati
- `HYPERLIQUID_NETWORK=testnet`
- `ENABLE_LIVE_TRADING=false`
- Stripe test mode
- shadow mode default
- Reown Allowed Origin dello staging configurato
- `REOWN_PROJECT_ID` impostato soltanto sul frontend
- Safari iPhone: MetaMask e Rabby via AppKit/WalletConnect
- Chrome Android: almeno un wallet EVM via AppKit/WalletConnect
- Desktop: injected/EIP-6963 e WalletConnect
- firma rifiutata, modal chiuso, cambio account e offline verificati
- console browser senza violazioni CSP inattese

Promuovere in production solo dopo P1/P2. Mainnet execute richiede P3.

## 16. Mainnet activation — triplo gate

Per inviare ordini mainnet devono essere vere tutte le condizioni:

1. `HYPERLIQUID_NETWORK=mainnet`
2. `ENABLE_LIVE_TRADING=true`
3. `system_flags.live_trading=true` in PostgreSQL, abilitato via endpoint SUPERADMIN con conferma `ENABLE MAINNET`.

Procedura consigliata:

```text
testnet shadow → testnet execute → mainnet shadow ≥ 1 settimana → canary mainnet → scale graduale
```

## 17. Backup PostgreSQL

Prima del mainnet:

1. Abilitare Railway Postgres PITR.
2. Eseguire un restore drill verso un nuovo Postgres sibling in staging.
3. Validare migrazioni, conteggi, audit e reconciliation.
4. Mantenere anche un export logico periodico fuori dal failure domain Railway se richiesto dalla policy aziendale/compliance.

Redis non è backuppato come source of truth: la sua perdita deve essere recuperabile tramite `scripts/rebuild_redis_queue.py` + reconciliation.

## 18. Rollback

### Code rollback

1. Railway → deployment history del service interessato.
2. Rollback/redeploy commit precedente.
3. Verificare `/health/ready` e control room.
4. Watcher/worker riconciliano lo stato persistente.

### Schema rollback

Preferire forward-fix con migration additive. Se un downgrade è indispensabile:

1. global pause nuove aperture;
2. backup/PITR point;
3. `alembic downgrade <revision>` manuale;
4. rollback del codice compatibile;
5. reconciliation completa;
6. riapertura controllata.

Non fare downgrade distruttivo mentre vecchio/nuovo deployment possono coesistere.

## 19. Primo deployment checklist

```text
[ ] GitHub Actions green
[ ] Railway Project creato
[ ] Postgres creato
[ ] Redis creato
[ ] frontend/api/watcher/worker collegati allo stesso repo
[ ] Root Directory impostate
[ ] Config File assoluti impostati
[ ] reference variables DB/Redis impostate
[ ] REOWN_PROJECT_ID impostato soltanto sul frontend
[ ] Reown Allowed Origins production/staging verificati
[ ] domini solo frontend/api
[ ] Stripe webhook firmato configurato
[ ] KMS privilege separation configurata
[ ] staging testnet deploy verde
[ ] login Safari iPhone verificato con wallet self-custodial
[ ] login Chrome Android verificato
[ ] login desktop injected + WalletConnect verificato
[ ] console CSP pulita durante il login wallet
[ ] Alembic head = codice atteso
[ ] /health/live e /health/ready verdi
[ ] watcher lease presente
[ ] worker heartbeat presente
[ ] Redis-loss recovery provata
[ ] testnet E2E/chaos completati
[ ] PITR restore drill completato
[ ] rollback provato
[ ] mainnet shadow report approvato
```
