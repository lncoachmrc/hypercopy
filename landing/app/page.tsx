"use client";

import { useEffect, useMemo, useState } from "react";
import {
  CONTACT_EMAIL,
  HYPERLIQUID_API_WALLET_URL,
  HYPERLIQUID_REFERRAL_URL,
  PRIVACY_POLICY_URL,
  TERMS_URL,
  TRAXION_APP_URL,
  traxionAsset,
} from "./config";

const navItems = [
  ["Come funziona", "#come-funziona"],
  ["Sicurezza", "#sicurezza"],
  ["Attivazione", "#attivazione"],
  ["Piani", "#piani"],
  ["Whitepaper", "#whitepaper"],
  ["FAQ", "#faq"],
] as const;

const architecture = [
  {
    number: "01",
    label: "Human signal",
    title: "Intelligence umana",
    text: "Analisi, ipotesi operative, segnali selezionati e lettura qualitativa del contesto.",
  },
  {
    number: "02",
    label: "Structured insight",
    title: "Capital Intelligence AI",
    text: "Pattern, priorità, profilo operativo ed efficienza del capitale ricondotti a uno schema controllato.",
  },
  {
    number: "03",
    label: "Deterministic control",
    title: "Risk Engine",
    text: "Regole verificabili consentono, riducono, negano o rinviano l'azione prima dell'aumento di esposizione.",
  },
  {
    number: "04",
    label: "On-chain action",
    title: "Execution Layer",
    text: "Target di posizione, ordini, riconciliazione e audit sull'account Hyperliquid dell'utente.",
  },
] as const;

const operationCards = [
  ["01", "Struttura l’intelligence", "Analisi, segnali e notizie validati convergono in un contesto operativo leggibile dalla catena decisionale."],
  ["02", "Definisce il target", "La strategia finalizzata viene tradotta nell’esposizione che l’account dovrebbe assumere, entro i limiti dell’utente."],
  ["03", "Calcola il sizing", "Il dimensionamento è proporzionale al capitale eleggibile e al moltiplicatore configurato nel Risk Engine."],
  ["04", "Controlla leva e margine", "Leva e modalità di margine vengono sincronizzate nel rispetto dei limiti del singolo mercato e del profilo rischio."],
  ["05", "Gestisce il ciclo posizione", "Aperture, incrementi, riduzioni, chiusure e inversioni vengono trattati come stati operativi distinti."],
  ["06", "Riconcilia e registra", "Ledger e stato reale di Hyperliquid vengono confrontati nel tempo; decisioni ed effetti restano tracciabili."],
] as const;

const securityItems = [
  ["Non-custodial", "Il capitale resta sull’account Hyperliquid dell’utente. TRAXION non riceve né custodisce i fondi."],
  ["Accesso wallet-native", "Il login usa una firma di messaggio gratuita. Sessione HttpOnly, protezione CSRF e richieste monouso riducono la superficie di fiducia."],
  ["Agent dedicato", "L’API Wallet Hyperliquid è separato dal wallet principale, nominato, verificabile, sostituibile e revocabile."],
  ["Controllo del rischio", "Risk Engine Basic e Pro applicano moltiplicatore, leva, esposizione, drawdown, perdita giornaliera, mercati e numero di posizioni."],
  ["Stati operativi distinti", "Pausa, modalità SHADOW, attivazione e chiusura delle posizioni gestite rimangono comandi separati."],
  ["Pseudonimia", "L’autenticazione minimizza i dati personali; l’indirizzo wallet resta pubblico e può essere collegato ad altre attività on-chain."],
] as const;

const plans = [
  {
    slug: "starter",
    name: "Starter",
    portfolio: "2.500 USD",
    monthly: 12,
    annual: 72,
    annualMonthly: 6,
    description: "Per iniziare con il trading ibrido automatizzato in modo semplice.",
  },
  {
    slug: "plus",
    name: "Plus",
    portfolio: "5.000 USD",
    monthly: 19.5,
    annual: 117,
    annualMonthly: 9.75,
    description: "Per portafogli in crescita che vogliono più capacità mantenendo la stessa strategia ibrida.",
  },
  {
    slug: "pro",
    name: "Pro",
    portfolio: "10.000 USD",
    monthly: 33,
    annual: 198,
    annualMonthly: 16.5,
    description: "Per utenti avanzati e portafogli più grandi che vogliono il massimo margine operativo e controllo del rischio.",
  },
] as const;

const faqs = [
  {
    q: "Che cos’è TRAXION?",
    a: "TRAXION è un sistema di trading ibrido. Collega intelligence umana, Capital Intelligence AI, Risk Engine ed execution layer per applicare sull’account Hyperliquid dell’utente un posizionamento operativo strutturato e controllato.",
  },
  {
    q: "TRAXION custodisce i miei fondi?",
    a: "No. I fondi rimangono sull’account Hyperliquid dell’utente. TRAXION usa un API Wallet/Agent autorizzato per le operazioni di trading previste dalla piattaforma.",
  },
  {
    q: "Quale chiave devo inserire?",
    a: "Esclusivamente la API Wallet Private Key generata per l’API Wallet/Agent su Hyperliquid, insieme al relativo API Wallet Address.",
  },
  {
    q: "Posso usare la chiave privata del wallet principale?",
    a: "No. TRAXION rifiuta una chiave che corrisponda al wallet principale. Seed phrase e chiave privata principale devono rimanere offline e sotto il tuo esclusivo controllo.",
  },
  {
    q: "Che cos’è un API Wallet o Agent Wallet?",
    a: "È una credenziale dedicata autorizzata dal wallet principale a firmare operazioni per l’account Hyperliquid. È separata dal wallet che detiene i fondi e può essere sostituita o revocata.",
  },
  {
    q: "L’API Wallet può prelevare i fondi?",
    a: "Il flusso ufficiale di prelievo Hyperliquid richiede la firma del wallet utente. TRAXION non espone funzioni di prelievo e usa l’Agent per le operazioni di trading implementate, come ordini e configurazione della leva.",
  },
  {
    q: "Che cosa fa la modalità SHADOW?",
    a: "Calcola target, sizing e controlli senza inviare ordini all’exchange. È lo stato consigliato per verificare configurazione e comportamento prima dell’attivazione.",
  },
  {
    q: "Come funziona il Risk Engine?",
    a: "Prima di aumentare l’esposizione valuta abilitazione, credenziale, pause, mercati consentiti, perdita giornaliera, drawdown, margine, leva, numero di posizioni e limiti di esposizione. Può consentire, ridurre, negare o rinviare l’azione.",
  },
  {
    q: "Il trial richiede una carta?",
    a: "No. Il trial di 14 giorni si attiva automaticamente al primo accesso e non richiede una carta, entro i limiti commerciali mostrati nella sezione Piani.",
  },
  {
    q: "TRAXION garantisce risultati o rendimenti?",
    a: "No. TRAXION è uno strumento tecnologico. Il trading di asset digitali e perpetual comporta rischio elevato e può determinare la perdita parziale o totale del capitale.",
  },
  {
    q: "TRAXION è affiliato con Hyperliquid?",
    a: "No. TRAXION è un progetto indipendente e non è affiliato, approvato o sponsorizzato da Hyperliquid.",
  },
  {
    q: "Come posso revocare l’API Wallet?",
    a: "Apri la pagina API Wallet del tuo account Hyperliquid e revoca o sostituisci l’Agent autorizzato. La revoca interrompe l’autorità operativa della credenziale.",
  },
] as const;

const whitepaperPages = Array.from(
  { length: 6 },
  (_, index) => traxionAsset(`/whitepaper/trx-wp-0${index + 1}.webp`),
);

function ExternalAppLink({ className, children }: { className?: string; children: React.ReactNode }) {
  return (
    <a className={className} href={TRAXION_APP_URL} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  );
}

function WhitepaperViewer() {
  const [page, setPage] = useState(0);
  const [zoom, setZoom] = useState(100);
  const total = whitepaperPages.length;

  useEffect(() => {
    [page - 1, page + 1].forEach((index) => {
      if (index >= 0 && index < total) {
        const preload = new Image();
        preload.src = whitepaperPages[index];
      }
    });
  }, [page, total]);

  const go = (next: number) => setPage(Math.max(0, Math.min(total - 1, next)));
  const blockViewerShortcut = (event: React.KeyboardEvent<HTMLElement>) => {
    const key = event.key.toLowerCase();
    if ((event.ctrlKey || event.metaKey) && ["s", "p", "c", "a"].includes(key)) {
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    if (event.key === "ArrowLeft") go(page - 1);
    if (event.key === "ArrowRight") go(page + 1);
  };

  return (
    <div
      className="whitepaper-viewer"
      tabIndex={0}
      role="region"
      aria-label="Viewer whitepaper TRAXION"
      onKeyDown={blockViewerShortcut}
      onContextMenu={(event) => event.preventDefault()}
      onDragStart={(event) => event.preventDefault()}
    >
      <div className="viewer-toolbar">
        <div className="viewer-pagination" aria-live="polite">
          <span className="viewer-dot" />
          Pagina <strong>{page + 1}</strong> di {total}
        </div>
        <div className="viewer-controls" aria-label="Controlli zoom">
          <button type="button" onClick={() => setZoom((value) => Math.max(75, value - 25))} aria-label="Riduci zoom">−</button>
          <button type="button" className="zoom-value" onClick={() => setZoom(100)} aria-label="Ripristina zoom">{zoom}%</button>
          <button type="button" onClick={() => setZoom((value) => Math.min(150, value + 25))} aria-label="Aumenta zoom">+</button>
        </div>
      </div>

      <div className="viewer-stage">
        <div className="viewer-page" style={{ width: `${zoom}%` }}>
          <img
            key={whitepaperPages[page]}
            src={whitepaperPages[page]}
            alt={`Pagina ${page + 1} della whitepaper TRAXION`}
            width="1547"
            height="2002"
            loading={page === 0 ? "eager" : "lazy"}
            decoding="async"
            draggable="false"
          />
        </div>
      </div>

      <div className="viewer-navigation">
        <button type="button" onClick={() => go(page - 1)} disabled={page === 0}>
          <span aria-hidden="true">←</span> Precedente
        </button>
        <div className="page-track" aria-hidden="true">
          {whitepaperPages.map((_, index) => (
            <i className={index === page ? "active" : ""} key={index} />
          ))}
        </div>
        <button type="button" onClick={() => go(page + 1)} disabled={page === total - 1}>
          Successiva <span aria-hidden="true">→</span>
        </button>
      </div>
    </div>
  );
}

function ActivationStep({ number, title, children }: { number: string; title: string; children: React.ReactNode }) {
  return (
    <article className="activation-step">
      <div className="step-rail"><span>{number}</span></div>
      <div className="step-content">
        <h3>{title}</h3>
        {children}
      </div>
    </article>
  );
}

export default function Home() {
  const [menuOpen, setMenuOpen] = useState(false);
  const [period, setPeriod] = useState<"monthly" | "annual">("monthly");

  useEffect(() => {
    const close = () => setMenuOpen(false);
    window.addEventListener("hashchange", close);
    return () => window.removeEventListener("hashchange", close);
  }, []);

  const faqSchema = useMemo(() => ({
    "@context": "https://schema.org",
    "@type": "FAQPage",
    mainEntity: faqs.map((item) => ({
      "@type": "Question",
      name: item.q,
      acceptedAnswer: { "@type": "Answer", text: item.a },
    })),
  }), []);

  return (
    <>
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(faqSchema) }} />
      <a className="skip-link" href="#main-content">Vai al contenuto</a>

      <header className="site-header">
        <div className="shell header-inner">
          <a className="brand" href="#top" aria-label="TRAXION, torna all'inizio">
            <img src={traxionAsset("/traxion-ai-copy-trading-logo.webp")} alt="TRAXION" width="2048" height="682" />
          </a>

          <nav className={menuOpen ? "nav open" : "nav"} aria-label="Navigazione principale">
            {navItems.map(([label, href]) => (
              <a key={href} href={href} onClick={() => setMenuOpen(false)}>{label}</a>
            ))}
          </nav>

          <ExternalAppLink className="button button-small header-cta">Accedi a TRAXION</ExternalAppLink>
          <button
            className="menu-button"
            type="button"
            aria-label={menuOpen ? "Chiudi menu" : "Apri menu"}
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((value) => !value)}
          >
            <span /><span />
          </button>
        </div>
      </header>

      <main id="main-content">
        <section className="hero" id="top">
          <div className="hero-grid" aria-hidden="true" />
          <div className="hero-glow hero-glow-one" aria-hidden="true" />
          <div className="hero-glow hero-glow-two" aria-hidden="true" />

          <div className="shell hero-layout">
            <div className="hero-copy">
              <div className="eyebrow-row"><span className="beta-badge">Beta</span><span>Hybrid trading intelligence</span></div>
              <img className="hero-logo" src={traxionAsset("/traxion-logo-completo.webp")} alt="TRAXION — Hyperliquid AI Trading Agent" width="1672" height="941" />
              <h1>Intelligenza ibrida.<br />Esecuzione deterministica.</h1>
              <p className="hero-lead">
                TRAXION collega analisti, segnali operativi, notizie e dati di mercato alla Capital Intelligence AI.
                Risk Engine ed execution layer trasformano il posizionamento finalizzato in azioni disciplinate,
                controllabili e verificabili.
              </p>
              <div className="hero-actions">
                <ExternalAppLink className="button">Prova TRAXION</ExternalAppLink>
                <a className="button button-ghost" href="#whitepaper">Leggi la whitepaper</a>
              </div>
              <div className="powered"><span>Powered by</span><strong>DigitalEmpower</strong></div>
            </div>

            <div className="hero-system" aria-label="Catena operativa TRAXION">
              <div className="system-head"><span>TRX / SYSTEM MAP</span><span className="status"><i /> Operational model</span></div>
              <div className="system-core"><div className="core-ring ring-one" /><div className="core-ring ring-two" /><div className="core-mark">X</div></div>
              <div className="system-flow">
                {architecture.map((item, index) => (
                  <div className="flow-row" key={item.number}><span>{item.number}</span><strong>{item.title}</strong><i className={index === architecture.length - 1 ? "active" : ""} /></div>
                ))}
              </div>
              <div className="system-foot"><span>Intelligence structures</span><span>Rules execute</span></div>
            </div>
          </div>
        </section>

        <section className="section" id="perche">
          <div className="shell split-intro">
            <div><p className="section-kicker">01 / Perché TRAXION</p><h2>Dall’informazione frammentata a un processo coerente.</h2></div>
            <div className="intro-copy">
              <p>I mercati crypto assorbono informazioni in pochi secondi. Analisi, segnali, notizie, dati e controllo del rischio vivono spesso in strumenti separati.</p>
              <p>TRAXION li porta dentro una catena strutturata: raccoglie intelligence, la organizza, applica limiti individuali e riconcilia continuamente obiettivo operativo e stato reale dell’account.</p>
              <div className="principles"><span>Strutturato</span><span>Disciplinato</span><span>Tracciabile</span></div>
            </div>
          </div>
        </section>

        <section className="section section-muted" id="come-funziona">
          <div className="shell">
            <div className="section-heading">
              <div><p className="section-kicker">02 / Architettura</p><h2>Quattro livelli. Una sola catena di responsabilità.</h2></div>
              <p>L’intelligenza elabora e struttura l’informazione. Le regole operative restano deterministiche, testabili e ripetibili.</p>
            </div>
            <div className="architecture-grid">
              {architecture.map((item) => (
                <article className="architecture-card" key={item.number}>
                  <div className="card-top"><span className="card-number">{item.number}</span><span className="card-label">{item.label}</span></div>
                  <h3>{item.title}</h3><p>{item.text}</p>
                </article>
              ))}
            </div>
          </div>
        </section>

        <section className="section execution-section" aria-labelledby="execution-title">
          <div className="shell">
            <div className="section-heading">
              <div><p className="section-kicker">03 / Funzionamento</p><h2 id="execution-title">Dal segnale strutturato allo stato reale dell’account.</h2></div>
              <p>Il motore lavora per target di posizione: calcola dove dovrebbe trovarsi l’account, sottrae la posizione reale ed esegue il delta necessario.</p>
            </div>
            <div className="operation-grid">
              {operationCards.map(([number, title, text]) => (
                <article className="operation-card" key={number}><span>{number}</span><h3>{title}</h3><p>{text}</p></article>
              ))}
            </div>
            <div className="audit-strip">
              <div><span>INPUT</span><strong>Intelligence validata</strong></div><i />
              <div><span>CONTROL</span><strong>Risk Engine</strong></div><i />
              <div><span>OUTPUT</span><strong>Delta verificabile</strong></div><i />
              <div><span>LOOP</span><strong>Reconciliation</strong></div>
            </div>
          </div>
        </section>

        <section className="section section-security" id="sicurezza">
          <div className="shell">
            <div className="security-intro">
              <div><p className="section-kicker">04 / Sicurezza e controllo</p><h2>Sovranità dell’utente, difesa a più livelli.</h2></div>
              <p>Wallet principale separato. Agent dedicato e revocabile. Limiti configurabili. Pause e chiusure distinte. Nessuna custodia dei fondi.</p>
            </div>

            <div className="critical-warning" role="alert">
              <div className="warning-mark">!</div>
              <div>
                <strong>Proteggi il wallet principale</strong>
                <p>TRAXION richiede esclusivamente la chiave privata dell’API Wallet/Agent generato su Hyperliquid. La seed phrase e la chiave privata del wallet principale non devono mai essere inserite o condivise.</p>
              </div>
            </div>

            <div className="security-grid">
              {securityItems.map(([title, text], index) => (
                <article className="security-card" key={title}><span>0{index + 1}</span><h3>{title}</h3><p>{text}</p></article>
              ))}
            </div>

            <div className="withdrawal-note">
              <strong>Prelievi fuori dal perimetro TRAXION</strong>
              <p>Il flusso ufficiale Hyperliquid richiede la firma del wallet utente per il prelievo. TRAXION non implementa funzioni di prelievo e usa la credenziale Agent per le operazioni di trading previste.</p>
            </div>
          </div>
        </section>

        <section className="section activation-section" id="attivazione">
          <div className="shell">
            <div className="section-heading activation-heading">
              <div><p className="section-kicker">05 / Attivazione</p><h2>Come attivare TRAXION.</h2></div>
              <p>Otto passaggi, dallo stesso wallet self-custody alla modalità SHADOW. Le azioni autenticate avvengono sempre nella webapp TRAXION o nell’interfaccia ufficiale Hyperliquid.</p>
            </div>

            <div className="activation-list">
              <ActivationStep number="01" title="Prepara un wallet self-custody">
                <p>Installa un wallet compatibile, per esempio MetaMask o Rabby, e crea o importa il wallet seguendo le istruzioni ufficiali del provider.</p>
                <ul><li>Conserva seed phrase e chiave privata principale offline.</li><li>TRAXION non richiederà mai questi dati.</li></ul>
              </ActivationStep>

              <ActivationStep number="02" title="Crea il profilo Hyperliquid">
                <p>Apri Hyperliquid e collega lo stesso wallet che userai successivamente per accedere a TRAXION.</p>
                <a className="inline-link" href={HYPERLIQUID_REFERRAL_URL} target="_blank" rel="sponsored noopener noreferrer">Apri Hyperliquid con referral DIGITALEMPOWER <span>↗</span></a>
                <small>Disclosure: il link contiene il referral code <code>DIGITALEMPOWER</code>.</small>
              </ActivationStep>

              <ActivationStep number="03" title="Prepara l’account operativo">
                <p>Completa l’onboarding mostrato dall’interfaccia ufficiale Hyperliquid. Se necessario per l’operatività, finanzia l’account seguendo esclusivamente reti, asset e procedure indicate nell’interfaccia ufficiale aggiornata.</p>
              </ActivationStep>

              <ActivationStep number="04" title="Genera un API Wallet Hyperliquid">
                <p>Apri la pagina API, crea un API Wallet — chiamato anche Agent Wallet — e usa come nome consigliato <code>hypercopy</code>, coerente con la webapp.</p>
                <div className="credential-box"><span>Conserva in modo sicuro</span><code>API Wallet Address</code><code>API Wallet Private Key</code></div>
                <a className="inline-link" href={HYPERLIQUID_API_WALLET_URL} target="_blank" rel="noopener noreferrer">Apri la pagina API Wallet ufficiale <span>↗</span></a>
              </ActivationStep>

              <ActivationStep number="05" title="Accedi a TRAXION">
                <p>Collega lo stesso wallet usato come account operativo Hyperliquid e firma il messaggio di autenticazione. La firma di login non comporta una transazione e non richiede gas.</p>
                <ExternalAppLink className="inline-link">Apri il login TRAXION <span>↗</span></ExternalAppLink>
              </ActivationStep>

              <ActivationStep number="06" title="Collega l’API Wallet">
                <p>In <strong>Configurazione</strong>, inserisci esattamente i due valori e premi <strong>Verifica e collega</strong>.</p>
                <div className="field-preview"><label>API Wallet Address<span>0x…</span></label><label>API Wallet Private Key<span>••••••••••••</span></label></div>
                <p className="step-note">TRAXION verifica che la chiave generi esattamente l’indirizzo inserito e che l’Agent sia autorizzato sul tuo account.</p>
              </ActivationStep>

              <ActivationStep number="07" title="Configura il Risk Engine">
                <p>Scegli una configurazione Basic già presente nella webapp oppure passa alla modalità Pro per i controlli avanzati.</p>
                <div className="risk-presets"><span>Prudente</span><span>Bilanciato</span><span>Strategia completa</span><span className="pro">Modalità Pro</span></div>
              </ActivationStep>

              <ActivationStep number="08" title="Attiva il servizio">
                <p>Il trial di 14 giorni si attiva automaticamente al primo accesso, senza carta. Puoi anche scegliere un abbonamento dall’area Piano.</p>
                <div className="shadow-callout"><strong>Inizia in modalità SHADOW</strong><span>Target, sizing e controlli vengono calcolati senza inviare ordini. Dopo le verifiche puoi selezionare “Attiva strategia”.</span></div>
              </ActivationStep>
            </div>
          </div>
        </section>

        <section className="section pricing-section" id="piani">
          <div className="shell">
            <div className="pricing-heading">
              <div><p className="section-kicker">06 / Trial e piani</p><h2>Capacità proporzionata al portafoglio operativo.</h2></div>
              <div className="period-toggle" role="group" aria-label="Periodicità dei prezzi">
                <button className={period === "monthly" ? "active" : ""} type="button" onClick={() => setPeriod("monthly")}>Mensile</button>
                <button className={period === "annual" ? "active" : ""} type="button" onClick={() => setPeriod("annual")}>Annuale</button>
                <span>−50%</span>
              </div>
            </div>

            <article className="trial-card">
              <div><span className="trial-label">Trial automatico</span><h3>14 giorni per verificare TRAXION.</h3><p>Nessuna carta richiesta. Accesso a Dashboard, Risk Engine Basic + Pro e trading ibrido entro limiti controllati.</p></div>
              <div className="trial-facts"><span><strong>1.000 USD</strong>portafoglio massimo</span><span><strong>3</strong>posizioni massime</span><span><strong>500 USD</strong>massimo per trade</span><span><strong>1×</strong>intensità massima</span></div>
              <ExternalAppLink className="button">Avvia il trial</ExternalAppLink>
            </article>

            <div className="pricing-grid">
              {plans.map((plan) => {
                const annual = period === "annual";
                const value = annual ? plan.annualMonthly : plan.monthly;
                return (
                  <article className={`pricing-card ${plan.slug === "plus" ? "featured" : ""}`} key={plan.slug}>
                    <div className="plan-cap">PORTAFOGLIO FINO A <strong>{plan.portfolio}</strong></div>
                    <h3>{plan.name}</h3>
                    <p>{plan.description}</p>
                    <div className="price"><strong>{value.toLocaleString("it-IT", { minimumFractionDigits: value % 1 ? 2 : 0, maximumFractionDigits: 2 })} USD</strong><span>/mese</span></div>
                    <div className="billing-note">{annual ? `fatturato ${plan.annual} USD/anno` : "fatturato mensilmente"}</div>
                    <ul><li>Trading ibrido multi-asset</li><li>Strategia analisti + sistemi AI</li><li>Risk Engine Basic + Pro</li><li>Esecuzione e riconciliazione</li></ul>
                    <ExternalAppLink className={plan.slug === "plus" ? "button" : "button button-ghost"}>Scegli {plan.name}</ExternalAppLink>
                  </article>
                );
              })}
            </div>
            <p className="pricing-footnote">Prezzi verificati nella configurazione corrente della webapp e indicati in USD. Il checkout effettivo avviene all’interno di TRAXION. La fatturazione annuale applica lo sconto del 50% previsto dal catalogo corrente.</p>
          </div>
        </section>

        <section className="section whitepaper-section" id="whitepaper">
          <div className="shell">
            <div className="whitepaper-intro">
              <div><p className="section-kicker">07 / Whitepaper</p><h2>La visione e l’architettura TRAXION, pagina per pagina.</h2></div>
              <div className="whitepaper-summary">
                <h3>Riepilogo accessibile</h3>
                <p>La whitepaper presenta la tesi del trading ibrido, l’origine del progetto nel 2024, il significato del nome TRAXION e la catena composta da intelligence umana, Capital Intelligence AI, Risk Engine ed execution layer.</p>
                <p>Descrive inoltre l’architettura AI multi-provider, il position targeting, la riconciliazione, il modello non-custodial, l’accesso wallet-native, la pseudonimia e il rollout disciplinato tra SHADOW, testnet e attivazione controllata.</p>
              </div>
            </div>
            <WhitepaperViewer />
          </div>
        </section>

        <section className="section faq-section" id="faq">
          <div className="shell">
            <div className="section-heading"><div><p className="section-kicker">08 / FAQ</p><h2>Domande essenziali, risposte verificabili.</h2></div><p>Sicurezza, credenziali, trial e funzionamento spiegati senza promesse di rendimento.</p></div>
            <div className="faq-list">
              {faqs.map((item, index) => (
                <details key={item.q}><summary><span>{String(index + 1).padStart(2, "0")}</span>{item.q}<i aria-hidden="true">+</i></summary><p>{item.a}</p></details>
              ))}
            </div>
          </div>
        </section>

        <section className="final-cta">
          <div className="shell final-cta-inner">
            <div><span>READ THE MARKET.</span><span>STRUCTURE THE SIGNAL.</span><span>EXECUTE WITH DISCIPLINE.</span></div>
            <ExternalAppLink className="button">Accedi a TRAXION</ExternalAppLink>
          </div>
        </section>
      </main>

      <footer className="site-footer">
        <div className="shell footer-main">
          <div className="footer-brand"><img src={traxionAsset("/traxion-ai-copy-trading-logo.webp")} alt="TRAXION" width="2048" height="682" /><p>Powered by DigitalEmpower</p></div>
          <div className="footer-links">
            <ExternalAppLink>Webapp TRAXION</ExternalAppLink>
            <a href={HYPERLIQUID_REFERRAL_URL} target="_blank" rel="sponsored noopener noreferrer">Hyperliquid — referral DIGITALEMPOWER</a>
            <a href={HYPERLIQUID_API_WALLET_URL} target="_blank" rel="noopener noreferrer">Gestisci API Wallet</a>
          </div>
          <div className="footer-pending" aria-label="Informazioni legali da configurare"><span>{PRIVACY_POLICY_URL}</span><span>{TERMS_URL}</span><span>{CONTACT_EMAIL}</span></div>
        </div>
        <div className="shell footer-disclaimer">
          <p>TRAXION è uno strumento tecnologico e non fornisce consulenza finanziaria, raccomandazioni personalizzate, sollecitazioni all’investimento o promesse di rendimento. Il trading di asset digitali e perpetual comporta un rischio elevato e può determinare la perdita parziale o totale del capitale. TRAXION è un progetto indipendente e non è affiliato, approvato o sponsorizzato da Hyperliquid.</p>
          <span>© 2026 TRAXION</span>
        </div>
      </footer>
    </>
  );
}
