# TRAXION — rete Hyperliquid per utente

## Decisione architetturale

La rete della strategia sorgente resta una proprietà del deployment (`HYPERLIQUID_MASTER_NETWORK`). La rete dell'account operativo diventa invece una proprietà persistente del singolo utente.

Ogni utente dispone di:

- `execution_network`: `testnet` oppure `mainnet`;
- `network_started_at`: inizio dell'epoca corrente della rete;
- un solo `TradingAccount` / API Wallet operativo alla volta.

Il default è `testnet`.

## Cambio rete

Il cambio TESTNET ↔ MAINNET è consentito solo quando sono vere tutte le condizioni seguenti:

1. strategia in `PAUSED`;
2. nessun API Wallet collegato;
3. nessuna posizione TRAXION gestita aperta;
4. nessun job `QUEUED`, `PROCESSING` o `RETRYING`;
5. nessuna esecuzione `SUBMITTING` o `UNKNOWN`.

Al cambio rete TRAXION:

- mantiene lo storico immutabile di esecuzioni, fill e audit;
- reinizializza il `PositionLedger`, perché rappresenta lo stato derivato dell'exchange corrente;
- reinizializza il `RiskState`, perché peak equity, perdita giornaliera e distanza dalla liquidazione appartengono alla rete corrente;
- apre una nuova `network_started_at` epoch;
- limita dashboard, PnL, equity e diagnostica operativa all'epoch corrente.

Dopo il cambio rete l'utente deve collegare un API/Agent Wallet autorizzato sulla rete selezionata.

## Routing runtime

Il master watcher continua a produrre un unico evento sorgente. Durante il fan-out ogni `CopyJob` riceve nel proprio context:

- `master_network`;
- `follower_network` dell'utente.

L'execution worker mantiene adapter Hyperliquid separati per TESTNET e MAINNET e seleziona quello coerente con la rete persistita dell'utente. Un job con una rete diversa dall'epoch corrente viene scartato come stale.

La riconciliazione periodica raggruppa gli utenti per rete e usa l'adapter corrispondente. Le API di activation, leverage e Control room seguono la stessa regola.

## Gate MAINNET

La selezione `mainnet` non abilita da sola ordini con fondi reali.

Per l'esecuzione MAINNET devono essere contemporaneamente soddisfatti:

1. utente con `execution_network=mainnet`;
2. API Wallet MAINNET verificato per il wallet operativo autenticato;
3. `ENABLE_LIVE_TRADING=true` nel deployment;
4. flag PostgreSQL `live_trading=true` abilitato esplicitamente da SUPERADMIN;
5. credenziale conservata con provider KMS esterno fuori dallo sviluppo;
6. account, entitlement e credenziale attivi;
7. Risk Engine in stato normale e limiti rispettati;
8. nessun global pause / emergency stop / altro blocco deterministico.

La UI può quindi permettere all'utente di preparare la configurazione MAINNET senza trasformare il toggle in un'autorizzazione a fare trading reale.

## Migrazione

Alembic `0009_user_execution_network` introduce `execution_network` e `network_started_at`. Gli account esistenti partono da `testnet`, coerentemente con l'attuale ambiente di staging e con il default fail-safe.

## Rollback

Il downgrade della migrazione rimuove le due colonne e ripristina il comportamento follower-network globale del codice precedente, previa rollback applicativa alla revisione compatibile. Prima del rollback devono essere arrestati i worker che dipendono da `0009_user_execution_network`.

## Vincoli di rollout

- staging prima di produzione;
- nessun `ENABLE_LIVE_TRADING=true` come effetto di questa modifica;
- nessuna abilitazione del flag DB `live_trading` come effetto di questa modifica;
- verifica esplicita di migrazione e rollback;
- verifica TESTNET completa prima della prova MAINNET shadow/read-only;
- mainnet con ordini reali solo dopo i gate previsti dalla specifica principale e autorizzazione esplicita.
