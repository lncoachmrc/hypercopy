# TRAXION — landing page ufficiale

Landing page italiana di TRAXION, mantenuta come progetto indipendente dalla
webapp operativa presente in `frontend/`.

## Sviluppo locale

Requisiti:

- Node.js `>=22.13.0`
- Linux con `flock`, `curl` e GNU `timeout` per gli script di build verificata

Comandi:

```bash
npm ci
npm run dev
npm test
```

`npm test` esegue la build, convalida l'artefatto e verifica i contenuti HTML
renderizzati, i metadati SEO, i collegamenti principali e le protezioni del
viewer della whitepaper.

## Configurazione

Gli URL modificabili sono centralizzati in `app/config.ts`:

- `TRAXION_APP_URL`
- `HYPERLIQUID_REFERRAL_URL`
- `HYPERLIQUID_API_WALLET_URL`
- `TRAXION_CANONICAL_URL`
- `PRIVACY_POLICY_URL`
- `TERMS_URL`
- `CONTACT_EMAIL`

Finché viene usato il dominio Railway di staging, la landing mostra il badge
`Beta`.

## Whitepaper

Il viewer usa esclusivamente pagine WebP rasterizzate e watermarkate in
`public/whitepaper/`. Il documento sorgente PDF/DOCX è intenzionalmente escluso
dalla repository e dall'output distribuito.

Le protezioni del viewer costituiscono una deterrenza best-effort: un browser o
un sistema operativo può sempre acquisire il contenuto visibile sullo schermo.

## Confini di deploy

Questa cartella non modifica il backend, la webapp, il checkout o la
configurazione Railway esistente. Il deployment della landing deve usare
`landing/` come directory radice del progetto.
