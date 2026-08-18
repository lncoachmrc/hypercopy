# TRAXION — rete Hyperliquid per utente

## Decisione architetturale

La rete della strategia sorgente resta una proprietà del deployment (`HYPERLIQUID_MASTER_NETWORK`). La rete dell'account operativo è una proprietà persistente del singolo utente.

Ogni utente dispone di:

- `execution_network`: `testnet` oppure `mainnet`;
- `network_started_at`: inizio dell'epoca corrente della rete;
- un solo `TradingAccount` / API Wallet operativo alla volta.

Il default è `testnet`.

## Cambio rete automatico

Il toggle TESTNET ↔ MAINNET è gestito direttamente dalla pagina Configurazione. TRAXION calcola continuamente lo stato di readiness e sblocca il cambio quando sono vere tutte le condizioni operative seguenti:

1. strategia in `PAUSED`;
2. nessuna posizione TRAXION gestita aperta;
3. nessun job `QUEUED`, `PROCESSING` o `RETRYING` nell'epoch corrente;
4. nessuna esecuzione `SUBMITTING` o `UNKNOWN` nell'epoch corrente.

Quando una o più condizioni non sono soddisfatte, il click sul toggle apre un popup con la checklist aggiornata. Il frontend aggiorna automaticamente lo stato e il toggle diventa disponibile appena i requisiti risultano completati.

L'API Wallet della rete corrente non è un prerequisito manuale. Quando il cambio rete viene eseguito, TRAXION:

- rimuove automaticamente il `TradingAccount` e la relativa credenziale della rete precedente;
- mantiene lo storico immutabile di esecuzioni, fill e audit;
- reinizializza il `PositionLedger`, perché rappresenta lo stato derivato dell'exchange corrente;
- reinizializza il `RiskState`, perché peak equity, perdita giornaliera e distanza dalla liquidazione appartengono alla rete corrente;
- apre una nuova `network_started_at` epoch;
- limita dashboard, PnL, equity e diagnostica operativa all'epoch corrente;
- mostra immediatamente la scheda per inserire `API Wallet Address` e `Private Key` della nuova rete.

Prima del passaggio a MAINNET la UI presenta un avviso esplicito che la rete utilizza fondi reali. La strategia resta in `PAUSED` dopo lo switch finché il nuovo API Wallet non viene configurato e l'utente non sceglie di riattivarla.

## Routing runtime

Il master watcher continua a produrre un unico evento sorgente. Durante il fan-out ogni `CopyJob` riceve nel proprio context:

- `master_network`;
- `follower_network` dell'utente.

L'execution worker mantiene adapter Hyperliquid separati per TESTNET e MAINNET e seleziona quello coerente con la rete persistita dell'utente. Un job con una rete diversa dall'epoch corrente viene scartato come stale.

La riconciliazione periodica raggruppa gli utenti per rete e usa l'adapter corrispondente. Le API di activation, leverage e Control room seguono la stessa regola.

## Gate di esecuzione MAINNET

La selezione `mainnet` configura la rete dell'utente; l'autorizzazione a inviare ordini reali resta separata dal toggle.

Per l'esecuzione MAINNET devono essere contemporaneamente soddisfatti:

1. utente con `execution_network=mainnet`;
2. API Wallet MAINNET verificato per il wallet operativo autenticato;
3. `ENABLE_LIVE_TRADING=true` nel deployment;
4. flag PostgreSQL `live_trading=true` abilitato esplicitamente da SUPERADMIN;
5. in produzione, credenziale conservata con provider KMS esterno;
6. account, entitlement e credenziale attivi;
7. Risk Engine in stato normale e limiti rispettati;
8. nessun global pause / emergency stop / altro blocco deterministico.

Lo staging può quindi verificare l'intero flusso di configurazione MAINNET senza richiedere KMS di produzione; l'invio di ordini reali resta comunque bloccato dai gate di esecuzione.

## Migrazione

Alembic `0009_user_execution_network` introduce `execution_network` e `network_started_at`. Gli account esistenti partono da `testnet`; il backfill usa `users.created_at` come epoch iniziale per preservare lo storico TESTNET già esistente.

## Rollback

Il downgrade della migrazione rimuove le due colonne e ripristina il comportamento follower-network globale del codice precedente, previa rollback applicativa alla revisione compatibile. Prima del rollback devono essere arrestati i worker che dipendono da `0009_user_execution_network`.

## Vincoli di rollout

- staging prima di produzione;
- nessun `ENABLE_LIVE_TRADING=true` come effetto di questa modifica;
- nessuna abilitazione del flag DB `live_trading` come effetto di questa modifica;
- verifica esplicita di migrazione e rollback;
- verifica del toggle TESTNET ↔ MAINNET, rimozione automatica della vecchia credenziale e nuova configurazione API Wallet in staging;
- mainnet con ordini reali solo dopo i gate previsti dalla specifica principale e autorizzazione esplicita.
