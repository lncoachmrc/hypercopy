# HyperCopy — Deployment GitHub → Railway → Hyperliquid

Questa è la procedura operativa completa per portare il monorepo da `git clone` a un ambiente Railway funzionante. Non usare provider applicativi alternativi: il runtime è Railway, il codice è GitHub, l'exchange è Hyperliquid.

## 1. Prerequisiti

- Repository GitHub con branch protetta `main`.
- Railway Project (consigliato piano con risorse/backup adeguati alla produzione).
- Account Stripe e tre Price ID (`basic`, `pro`, `enterprise`).
- Wallet master Hyperliquid: **serve solo l'indirizzo pubblico**.
- Per ogni follower: account Hyperliquid + agent/API wallet nominato `hypercopy`.
- Per mainnet production: KMS esterno. Questa repo implementa `aws_kms`; è l'unica dipendenza infrastrutturale esterna prevista per la custodia avanzata della KEK.

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

Il flusso di rete è:

```text
Internet
  ├─ frontend
  └─ api
       └─ Railway Private Network
          ├─ Postgres
          └─ Redis

master-watcher ──private──> Postgres/Redis ──outbound──> Hyperliquid
execution-worker ─private─> Postgres/Redis ──outbound──> Hyperliquid
```

Frontend non necessita accesso alla private network: è statico e chiama l'API via HTTPS/WSS pubblico.

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

## 9. Variabili frontend

Sul service `frontend`:

```text
API_BASE_URL=https://api.example.com/api/v1
WS_URL=wss://api.example.com/api/v1/ws/events
```

Sono configurazioni pubbliche, generate a runtime in `/config.js`; non sono secret build-time.

## 10. Domains

Assegnare domini pubblici solo a:

- `frontend`: `app.example.com`
- `api`: `api.example.com`

Usare la stessa registrable domain per garantire il comportamento SameSite previsto dalle sessioni. Configurare Stripe webhook verso:

```text
https://api.example.com/api/v1/webhooks/stripe
```

Non assegnare public domain a watcher, worker, Postgres o Redis.

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
[ ] domini solo frontend/api
[ ] Stripe webhook firmato configurato
[ ] KMS privilege separation configurata
[ ] staging testnet deploy verde
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
