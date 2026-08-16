# HyperCopy — Disaster Recovery & Operations Runbook

Principio: **PostgreSQL + stato reale Hyperliquid sono la base di recovery**. Redis è ricostruibile. In dubbio, preferire perdita temporanea di liveness a un ordine duplicato.

## Severity

- `SEV-1`: rischio di ordini errati/duplicati, compromissione chiavi, DB non affidabile.
- `SEV-2`: copytrading fermo/degradato ma stato persistente integro.
- `SEV-3`: dashboard/metriche/billing degradati senza impatto immediato sull'esecuzione.

## Emergency controls

`Global pause` ed `Emergency stop` bloccano nuove aperture/incrementi ma **non le riduzioni**. `Close positions` è un'operazione separata: non liquidare automaticamente il book quando l'operatore chiede solo di fermare nuove esposizioni.

## 1. Hyperliquid indisponibile

1. Aprire `/admin/system`; verificare WS reconnect, queue age e `hl_429_count`.
2. Non cancellare job PG/Redis.
3. Il sistema deve accumulare job durevoli o fallire senza effetto esterno.
4. Se outage > 30 min, considerare Global pause per evitare ingressi tardivi a prezzi ormai diversi.
5. Al recovery: risolvere execution `SUBMITTING/UNKNOWN`, reconciliation master/follower, poi riprendere.

## 2. PostgreSQL indisponibile

Comportamento atteso: `/health/ready` rosso; API non pronta; watcher/worker non possono scrivere intenti e quindi **non devono eseguire ordini**.

Recovery:

1. ripristinare Postgres;
2. verificare Alembic revision;
3. controllare `executions` in `SUBMITTING/UNKNOWN`;
4. reconciliation completa;
5. verificare lease watcher e worker heartbeat;
6. riaprire nuove esposizioni solo dopo consistenza confermata.

## 3. Redis perso / flush totale

Redis non contiene verità persistente.

1. lasciare Postgres intatto;
2. ripristinare Redis;
3. eseguire `python scripts/rebuild_redis_queue.py` nel contesto backend con DATABASE_URL/REDIS_URL corretti;
4. riavviare worker se necessario;
5. eseguire reconciliation completa;
6. verificare che i job PG terminali non siano ri-eseguiti.

Criterio di successo: zero ordini duplicati; queue Redis ricostruita dai soli job PG non terminali.

## 4. Worker crash durante ordine

Caso critico: `Execution(SUBMITTING)` è stata committata prima dell'effetto esterno.

1. non modificare manualmente il job;
2. il nuovo worker trova l'Execution persistente e interroga Hyperliquid per `cloid`;
3. se l'exchange dà stato terminale, persistere e continuare;
4. se resta `unknownOid`/ambiguo, **non blind-resubmit**;
5. aprire incidente e risolvere contro stato reale/fill/posizione prima di creare un nuovo intento.

È preferibile una replica mancata temporaneamente a un doppio ordine.

## 5. Watcher crash / doppio watcher

1. controllare riga `watcher_lease` e fencing token;
2. il leader valido rinnova ogni 5s, TTL 15s;
3. dopo SIGTERM clean il lease viene rilasciato immediatamente;
4. se il vecchio processo torna dopo scadenza, il token/expiry lo recinta;
5. il nuovo leader esegue replay dal checkpoint PG e deduplica su `exchange_event_id`.

Se appare `MASTER_REPLAY_GAP_UNPROVEN`, il sistema deve restare in global pause finché una reconciliation manuale non dimostra lo stato.

## 6. Queue backlog

Allarmi:

- `queue_depth > 500`
- `oldest_job_age_seconds > 60`
- `execution_latency_ms p95 > 5000`

Azioni:

1. distinguere rate-limit da worker lento;
2. controllare `hl_rate_weight_used`;
3. non scalare worker se il collo di bottiglia è il budget Hyperliquid;
4. se CPU/DB sono il limite e rate budget ha margine, scalare `execution-worker` 1→N;
5. verificare consumer identity distinta e heartbeat;
6. dopo stabilizzazione reconciliation.

## 7. Rate limit Hyperliquid

Se `hl_rate_weight_used > 80%` o `hl_429_count > 0`:

1. Global pause nuove aperture se la pressione persiste;
2. dare priorità a ordini/riduzioni e reconciliation critica;
3. ridurre frequenza reconciliation non urgente;
4. non aggiungere repliche per aggirare il budget condiviso;
5. misurare l'egress Railway reale prima di cambiare i parametri.

## 8. Migration fallita

Il Pre-Deploy API deve bloccare il rollout.

1. non avviare migration dai worker;
2. conservare il deployment precedente;
3. leggere errore Alembic;
4. correggere con nuova migration/forward-fix;
5. per migration distruttiva: backup/PITR prima di qualsiasi retry;
6. validare staging prima di production.

## 9. Migration riuscita, codice nuovo fallisce

Se migration è additive:

1. rollback Railway al commit precedente;
2. non fare downgrade DB automaticamente;
3. verificare compatibilità vecchio codice ↔ schema nuovo;
4. forward-fix successivo.

Se migration non è backward-compatible, la release non avrebbe dovuto passare il gate: Global pause e procedura DB restore/downgrade controllata.

## 10. Railway full project restart

Ordine logico di recovery:

```text
Postgres disponibile
→ schema revision valida
→ Redis disponibile
→ API ready
→ watcher acquisisce lease + replay
→ worker risolve SUBMITTING/UNKNOWN
→ reconciliation
→ resume processing
```

L'ordine fisico di start Railway può differire; schema guard/health/readiness devono impedire operazioni premature.

## 11. PostgreSQL PITR restore

1. fermare nuove aperture (`emergency_stop=true` o global pause);
2. scegliere timestamp precedente all'incidente;
3. Railway PITR crea un **nuovo Postgres sibling**, senza toccare l'originale;
4. collegare prima uno staging/clone dell'app al DB ripristinato;
5. `alembic current`, integrity query, conteggi e audit;
6. confrontare `position_ledger` con Hyperliquid reale;
7. risolvere ogni `SUBMITTING/UNKNOWN`;
8. solo dopo cut-over delle `DATABASE_URL` reference variables;
9. reconciliation completa;
10. riapertura controllata.

Non distruggere il DB originale finché il restore non è validato.

## 12. Stripe webhook outage/duplicato

- Redirect Checkout non concede entitlement.
- Eventi Stripe hanno `event_id` unique in PG.
- In caso di retry/duplicato: deve essere no-op.
- Dopo outage: lasciare che Stripe ritenti; verificare `stripe_events` e subscription locale.

## 13. Agent wallet in scadenza

- 30 giorni: warning/alert.
- 7 giorni: stato `EXPIRING`, banner persistente.
- scaduto: `EXPIRED` + halt credenziale; nessuna nuova esecuzione firmabile.

Chiedere all'utente di creare/approvare un nuovo agent `hypercopy`, poi ricollegare. Non chiedere mai la main wallet key.

## 14. Compromissione agent key / KMS

SEV-1:

1. Emergency stop nuove esposizioni.
2. Identificare utenti/credential coinvolti.
3. Chiedere revoca immediata dell'agent su Hyperliquid: è l'azione che invalida una chiave eventualmente esfiltrata.
4. Ruotare KMS/KEK e rewrap DEK se il wrapping key è coinvolto.
5. Onboarding nuovi agent.
6. Audit log + post-mortem + notification policy applicabile.
7. Non riattivare prima di security review.

## 15. Backup policy

Production minima:

| Asset | Meccanismo | Frequenza/retention | Verifica |
|---|---|---|---|
| Postgres | Railway PITR | finestra disponibile dal piano/config | restore drill mensile |
| Postgres logical | `pg_dump` verso storage esterno/compliance | giornaliero, es. 30 giorni | restore trimestrale |
| Pre-migration | snapshot/PITR point + dump se distruttiva | ogni migration critica | prima del deploy |
| KMS | policy del provider + key lifecycle | tutte le versioni necessarie | rotation drill |
| Redis | nessun backup autorevole | n/a | chaos test perdita totale |

## 16. Recovery test calendar

Prima del mainnet e poi periodicamente:

- perdita Redis completa;
- worker kill fra durable intent e risposta exchange;
- due watcher concorrenti;
- Hyperliquid WS disconnect/reconnect;
- PostgreSQL unavailable 30s;
- Railway redeploy sotto carico;
- PITR restore;
- rollback code;
- rotazione KMS;
- Stripe duplicate webhook.

Criterio trading: **zero duplicati e convergenza allo stato corretto entro due cicli di reconciliation** per i casi coperti.
