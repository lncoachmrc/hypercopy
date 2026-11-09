# TRAXION Strategy Lab — Replay orario causale, 9 novembre 2026

**Oggetto:** verificare se una chiusura anticipata, modellata come semplice scenario di ricerca, avrebbe migliorato il P&L rispetto alle chiusure master osservate. **Non è una modifica delle regole operative del bot**, né un test del comportamento di un modello LLM.

## Fonti

- Esportazione locale dell'utente `10_master_events_history.csv`: **1.766 fill / 81 asset**, dal **28 agosto al 3 ottobre 2026**; il file resta sul computer dell'analista, **non nel repository, nei log o su Railway**.
- Prezzi OHLCV di Hyperliquid, endpoint pubblico `POST https://api.hyperliquid.xyz/info`, tipo `candleSnapshot` (sola lettura). Documentazione ufficiale: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
- API: **massimo 5.000 candele più recenti per asset/intervallo**. L'intervallo `15m` copre ~52 giorni, insufficienti per tutto l'archivio alla data corrente; `1h` copre ~208 giorni e permette di interrogare l'intero periodo. Una candela può essere assente o un asset non più quotato; non interpolare.
- Il workflow GitHub è limitato al branch di ricerca e genera **solo dati di mercato pubblici**, senza wallet, credenziali, segreti o ordini.

## Ambiente isolato Railway

Creato nel workspace originale un **nuovo progetto privato** `TRAXION Strategy Lab`:
- Project ID: `b5aaa587-b306-48fc-96a5-2c4a8e6faff6`
- Environment: `staging` `26d91c72-7ad5-4a2b-b8bc-8cc421a57bdb`
- Stato iniziale verificato: **zero servizi, zero variabili condivise, zero volumi, zero bucket, zero deployment**.
- Questo progetto resta **senza deployment** finché non saranno preparati DB/worker privi di capacità di trading. Non clonare la produzione o `risex-e2e`; non copiare segreti, agent wallet o URL database mainnet.

## Procedura riproducibile

1. Aprire la GitHub Action `Strategy Lab public candle archive (read-only)` relativa alla PR: esegue test offline e raccolta pubblica `1h` degli 81 asset.
2. Scaricare l'artifact `traxion-public-hourly-candles-<commit>`, che contiene `candles.jsonl` e `manifest.json` con `sha256`, copertura per asset e cause di eventuali mancanze.
3. Usare il file CSV master **soltanto in locale**:

```bash
python scripts/strategy_lab_candle_replay.py \
  --fills /percorso/privato/10_master_events_history.csv \
  --candles /percorso/artifact-estratto \
  --output /percorso/privato/strategy_lab_replay.json \
  --fee-bps 5 --exit-slip-bps 10
```

Le assunzioni di 5 bps commissione per fill e 10 bps di slippage sull'uscita ipotetica sono **scenari di stress**, non tariffe effettive dell'account o quotazioni eseguibili. Il funding e il costo LLM restano fuori dal modello. Il report conserva i totali netti stimati, i cicli coperti ed esclusi e le differenze rispetto alle uscite master.

## Regole causali del replay

- Ricostruire solo cicli con entrata e uscita osservate, includendo apertura, riduzione e inversione; escludere i cicli sinistro-destramente censurati. Stesso timestamp richiede ordinamento compatibile con la catena delle posizioni.
- Alla chiusura di ogni barra `1h` **già terminata mentre la posizione era aperta**, valutare P&L netto approssimato, al prezzo close con fee e slippage ipotetici.
- Calcolare il **massimo P&L campionato fino a quel momento**. Un segnale scenariale compare dopo almeno due barre, peak stimato >1 USDC e profitto corrente >0,5 USDC quando il giveback raggiunge **25%, 40% o 60%**. Sono parametri esplorativi, non una strategia scelta, né regole nuove di AI Profit Exit.
- Uscita **solo alla chiusura della barra successiva** (ritardo conservativo). Escludere un segnale se tra decisione e quel momento il master apre, riduce, chiude o inverte; non usare dati futuri per decidere.
- Il controfattuale assume che dopo una chiusura totale non si rientri nel medesimo ciclo. L'effettiva eseguibilità (spread, book, slippage, liquidity, latency, posizione follower) **non è dimostrata**.
- Confrontare lo stesso campione coperto, riportando anche gli esclusi. La selezione dei parametri avviene a posteriori: **nessuna validazione out-of-sample**. Non inferire P&L recuperabile o robustezza statistica dai massimi retrospettivi.

## Stop/go

**STOP** se mancano quote, intervalli o coerenza temporale; non inventare valori e non trasformare una candela high/low in un'uscita eseguibile.

**GO solo per altre analisi:** se i risultati sono coerenti, raccogliere osservazioni *prospettiche* di Profit Retention SHADOW SHORT sulla nuova telemetria, costi reali e dati di prezzo più frequenti. Confrontare HOLD/CLOSE/ABSTAIN e P&L netto realizzato su coorti fuori campione.

**NON fare merge su main, deployment in produzione o trading Hyperliquid mainnet** senza nuova autorizzazione esplicita. Il CI di questa PR può risultare rosso per audit baseline delle dipendenze già affrontate altrove; il workflow dedicato con dati pubblici e i propri test hanno un esito separato.
