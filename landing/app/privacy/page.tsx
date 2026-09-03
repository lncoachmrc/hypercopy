import type { Metadata } from "next";
import styles from "./privacy.module.css";

export const metadata: Metadata = {
  title: "Privacy Policy | TRAXION",
  description:
    "Informativa privacy TRAXION su dati trattati, finalità, sicurezza, provider tecnologici e diritti degli interessati.",
  robots: { index: true, follow: true },
  alternates: {
    canonical: "https://traxion.lucianonovello.com/privacy/",
  },
};

const purposes = [
  ["Creazione e gestione dell’account", "Esecuzione del contratto o misure precontrattuali"],
  ["Autenticazione tramite wallet", "Esecuzione del contratto e sicurezza del servizio"],
  ["Collegamento dell’Agent/API Wallet", "Esecuzione del contratto"],
  ["Execution, reconciliation e Risk Engine", "Esecuzione del contratto"],
  ["Gestione dell’abbonamento", "Esecuzione del contratto"],
  ["Fatturazione e obblighi fiscali", "Obbligo legale"],
  ["Sicurezza, prevenzione abusi e incident response", "Interesse legittimo del Titolare"],
  ["Audit e tracciabilità degli eventi", "Interesse legittimo, sicurezza e tutela dei diritti"],
  ["Assistenza all’utente", "Esecuzione del contratto o misure precontrattuali"],
  ["Capital Intelligence e funzionalità AI richieste", "Esecuzione del servizio"],
  ["Cookie o tecnologie non essenziali eventualmente introdotte", "Consenso, quando richiesto"],
] as const;

export default function PrivacyPolicyPage() {
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.headerInner}>
          <a className={styles.brand} href="/">TRAXION</a>
          <a className={styles.back} href="/">← Torna al sito</a>
        </div>
      </header>

      <main className={styles.main}>
        <div className={styles.hero}>
          <p className={styles.kicker}>Informativa sul trattamento dei dati personali</p>
          <h1>Privacy Policy</h1>
          <p className={styles.lead}>
            La presente informativa descrive come vengono trattati i dati personali degli utenti che visitano
            TRAXION, accedono alla web application o utilizzano i relativi servizi.
          </p>
        </div>

        <div className={styles.notice}>
          <strong>Stato dei dati del Titolare</strong>
          Dati identificativi completi del titolare in corso di completamento. Prima della messa in esercizio
          commerciale definitiva saranno pubblicati la denominazione fiscale, il NIF/NIE, il domicilio
          professionale e il contatto privacy dedicato.
        </div>

        <div className={styles.content}>
          <section className={styles.section}>
            <h2>1. Ambito dell’informativa</h2>
            <p>La presente Privacy Policy si applica in particolare ai servizi pubblicati sui domini:</p>
            <ul>
              <li>traxion.lucianonovello.com</li>
              <li>app.traxion.lucianonovello.com</li>
              <li>api.traxion.lucianonovello.com</li>
            </ul>
            <p>
              Il servizio commerciale e tecnologico descritto nella presente informativa è denominato TRAXION.
            </p>
          </section>

          <section className={styles.section}>
            <h2>2. Titolare del trattamento</h2>
            <p>
              Il Titolare del trattamento è il soggetto che gestisce TRAXION. I dati identificativi completi del
              Titolare, inclusi denominazione fiscale, NIF/NIE, domicilio professionale e indirizzo email privacy,
              sono in corso di completamento e saranno pubblicati in questa sezione appena disponibili.
            </p>
          </section>

          <section className={styles.section}>
            <h2>3. Principi del trattamento</h2>
            <p>
              TRAXION tratta i dati personali secondo principi di liceità, correttezza, trasparenza,
              minimizzazione, limitazione delle finalità, esattezza, limitazione della conservazione, integrità e
              riservatezza. Vengono raccolti soltanto i dati ragionevolmente necessari per fornire, proteggere e
              amministrare il servizio.
            </p>
            <p>
              TRAXION non richiede la seed phrase né la chiave privata del wallet principale dell’utente.
            </p>
          </section>

          <section className={styles.section}>
            <h2>4. Dati trattati</h2>

            <h3>4.1 Identificazione tramite wallet</h3>
            <p>
              L’accesso a TRAXION avviene principalmente attraverso autenticazione crittografica tramite wallet.
              Possono essere trattati indirizzo pubblico del wallet, messaggi di autenticazione, nonce o challenge,
              informazioni necessarie alla verifica della firma, timestamp di accesso, identificatori di sessione,
              ruolo e stato dell’account.
            </p>
            <p>
              L’indirizzo blockchain è un identificatore pseudonimo e può costituire un dato personale quando può
              essere collegato direttamente o indirettamente a una persona fisica.
            </p>

            <h3>4.2 Dati tecnici e di sicurezza</h3>
            <p>
              Possono essere trattati indirizzo IP o sue rappresentazioni crittografiche, user agent, tipo di
              browser, timestamp, identificativi tecnici, correlation ID, eventi di autenticazione e sicurezza,
              log applicativi, tentativi di accesso e informazioni necessarie al rate limiting e alla prevenzione
              di abusi. Gli indirizzi IP destinati all’audit applicativo possono essere sottoposti a HMAC prima
              della conservazione.
            </p>

            <h3>4.3 Dati relativi all’account Hyperliquid</h3>
            <p>
              Quando l’utente collega TRAXION al proprio account Hyperliquid possono essere trattati indirizzo
              dell’account, indirizzo dell’Agent/API Wallet autorizzato, network utilizzato, posizioni, ordini,
              execution, leverage, esposizione, margin state, profit and loss, storico operativo, parametri di
              rischio e altre informazioni necessarie alla sincronizzazione e riconciliazione delle posizioni.
            </p>

            <h3>4.4 Credenziale Agent/API Wallet</h3>
            <p>
              Per consentire l’esecuzione automatizzata, l’utente può fornire una chiave privata relativa
              esclusivamente a un Agent/API Wallet dedicato. La credenziale viene utilizzata per le funzioni
              autorizzate dal servizio e protetta mediante envelope encryption con chiave dati casuale per record e
              AES-256-GCM. La credenziale in chiaro viene mantenuta solamente per il tempo tecnicamente necessario
              alle operazioni di cifratura iniziale o firma autorizzata.
            </p>
            <p>
              La seed phrase e la chiave privata del wallet principale non devono essere comunicate a TRAXION.
            </p>

            <h3>4.5 Dati operativi e Risk Engine</h3>
            <p>
              Possono essere trattati configurazioni operative, moltiplicatori, limiti di rischio, asset consentiti
              o esclusi, esposizione massima, drawdown, limiti di perdita, stato delle posizioni e dati necessari a
              riconciliazione e controllo dell’esecuzione.
            </p>

            <h3>4.6 Pagamenti e abbonamenti</h3>
            <p>
              Per le funzionalità a pagamento possono essere trattati identificativi cliente e abbonamento,
              piano sottoscritto, stato dell’abbonamento, date di rinnovo o scadenza, eventi associati al pagamento
              e informazioni amministrative o di fatturazione necessarie. I dati completi della carta sono gestiti
              dal prestatore di pagamento e non devono essere memorizzati direttamente da TRAXION.
            </p>

            <h3>4.7 Funzionalità AI</h3>
            <p>
              TRAXION può utilizzare sistemi di intelligenza artificiale per produrre analisi strutturate relative
              alla strategia, all’attività osservata e ad altri parametri del servizio. A seconda della
              configurazione possono essere utilizzati provider esterni di modelli linguistici. Chiavi private dei
              wallet, seed phrase, token di sessione e credenziali crittografiche non devono essere trasmessi ai
              provider AI. Il componente AI ha funzione analitica e non possiede direttamente l’autorità tecnica
              necessaria per firmare o inviare ordini.
            </p>

            <h3>4.8 Comunicazioni volontarie</h3>
            <p>
              Se l’utente contatta il Titolare possono essere trattati indirizzo email, nome eventualmente
              comunicato, contenuto della richiesta, allegati e cronologia dell’assistenza.
            </p>
          </section>

          <section className={styles.section}>
            <h2>5. Finalità e basi giuridiche</h2>
            <div className={styles.tableWrap}>
              <table className={styles.table}>
                <thead>
                  <tr><th>Finalità</th><th>Base giuridica</th></tr>
                </thead>
                <tbody>
                  {purposes.map(([purpose, basis]) => (
                    <tr key={purpose}><td>{purpose}</td><td>{basis}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <section className={styles.section}>
            <h2>6. Sicurezza e protezione delle credenziali</h2>
            <p>TRAXION applica misure tecniche e organizzative finalizzate a proteggere le informazioni trattate.</p>
            <ul>
              <li>cifratura delle credenziali Agent ed envelope encryption;</li>
              <li>chiave dati separata per credenziale e AES-256-GCM;</li>
              <li>separazione tra servizi API ed execution;</li>
              <li>autenticazione wallet-native e session cookie protetti;</li>
              <li>protezione CSRF, rate limiting e controlli di autorizzazione;</li>
              <li>audit log e separazione tra componente AI ed execution;</li>
              <li>limitazione dell’accesso ai segreti operativi.</li>
            </ul>
            <p>Nessuna misura tecnica può garantire sicurezza assoluta.</p>
          </section>

          <section className={styles.section}>
            <h2>7. Processi automatizzati e intelligenza artificiale</h2>
            <p>
              TRAXION comprende processi automatizzati che possono analizzare lo stato delle posizioni, determinare
              target operativi, calcolare delta, applicare limiti di rischio, sincronizzare leverage, riconciliare
              lo stato interno con quello dell’exchange e inviare operazioni autorizzate in base alla configurazione
              dell’utente.
            </p>
            <p>
              Le componenti AI possono contribuire all’analisi e alla strutturazione delle informazioni. L’AI non
              dispone direttamente delle credenziali necessarie per l’execution e non costituisce l’autorità finale
              di firma degli ordini. L’esecuzione operativa rimane sottoposta a regole deterministiche, controlli di
              rischio e autorizzazioni tecniche.
            </p>
          </section>

          <section className={styles.section}>
            <h2>8. Cookie e tecnologie tecniche</h2>
            <p>
              La web application utilizza cookie strettamente necessari al funzionamento e alla sicurezza del
              servizio, tra cui cookie di sessione, refresh, CSRF e identificatori necessari alla sicurezza
              dell’autenticazione. Questi cookie non hanno finalità pubblicitaria.
            </p>
            <p>
              Qualora in futuro vengano introdotti cookie analitici, pubblicitari o altre tecnologie non
              strettamente necessarie, il relativo trattamento sarà gestito secondo le regole applicabili sul
              consenso.
            </p>
          </section>

          <section className={styles.section}>
            <h2>9. Destinatari e fornitori tecnologici</h2>
            <p>I dati possono essere trattati da fornitori necessari alla gestione del servizio, tra cui:</p>
            <ul>
              <li><strong>Railway</strong>, per hosting e infrastruttura applicativa;</li>
              <li><strong>GitHub / GitHub Pages</strong>, per repository e pubblicazione della landing;</li>
              <li><strong>Stripe</strong>, per pagamenti e abbonamenti quando attivati;</li>
              <li><strong>Hyperliquid</strong>, per stato account ed esecuzione delle operazioni autorizzate;</li>
              <li><strong>OpenAI, Anthropic e/o DeepSeek</strong>, quando utilizzati dalle funzioni di Capital Intelligence;</li>
              <li><strong>Reown / WalletConnect e wallet compatibili</strong>, quando utilizzati per la connessione del wallet.</li>
            </ul>
            <p>
              Il ruolo privacy preciso di ciascun provider dipende dal servizio effettivamente utilizzato e dai
              relativi accordi contrattuali. Possono inoltre ricevere informazioni le autorità pubbliche o
              giudiziarie quando previsto dalla legge.
            </p>
          </section>

          <section className={styles.section}>
            <h2>10. Trasferimenti internazionali</h2>
            <p>
              Alcuni fornitori tecnologici possono essere stabiliti o trattare informazioni fuori dallo Spazio
              Economico Europeo. Quando applicabile, i trasferimenti internazionali saranno effettuati sulla base
              degli strumenti previsti dal GDPR, incluse decisioni di adeguatezza, clausole contrattuali standard o
              altre garanzie applicabili.
            </p>
          </section>

          <section className={styles.section}>
            <h2>11. Conservazione</h2>
            <p>I dati vengono conservati per il periodo necessario alla finalità per cui sono stati raccolti.</p>
            <ul>
              <li>account e configurazioni: per la durata del rapporto e per gli ulteriori periodi legittimamente necessari;</li>
              <li>sessioni e autenticazione: per la durata tecnicamente necessaria alla gestione e sicurezza;</li>
              <li>credenziali Agent cifrate: fino a revoca, sostituzione, scadenza o cancellazione dell’account, salvo obblighi applicabili;</li>
              <li>execution e audit: per il periodo necessario a tracciabilità, sicurezza e gestione di contestazioni;</li>
              <li>assistenza: fino alla conclusione della richiesta e per eventuali esigenze di tutela;</li>
              <li>documentazione amministrativa e contabile: per i periodi imposti dalla normativa applicabile.</li>
            </ul>
          </section>

          <section className={styles.section}>
            <h2>12. Origine dei dati</h2>
            <p>
              I dati possono provenire direttamente dall’utente, dal wallet collegato, dall’account Hyperliquid,
              dall’attività generata mediante il servizio, da blockchain o infrastrutture pubblicamente
              accessibili, dai provider di pagamento e dai sistemi tecnici utilizzati per sicurezza e logging.
            </p>
          </section>

          <section className={styles.section}>
            <h2>13. Natura del conferimento</h2>
            <p>
              La comunicazione dei dati necessari all’autenticazione, all’account, alla connessione Hyperliquid e
              all’execution è necessaria per utilizzare le relative funzionalità. L’utente può scegliere di non
              fornire tali dati, ma alcune funzioni o l’intero servizio potrebbero non essere disponibili.
            </p>
          </section>

          <section className={styles.section}>
            <h2>14. Diritti dell’interessato</h2>
            <p>Nei casi previsti dalla normativa applicabile, l’utente può esercitare i diritti di:</p>
            <ul>
              <li>accesso, rettifica e cancellazione;</li>
              <li>limitazione del trattamento e opposizione;</li>
              <li>portabilità dei dati;</li>
              <li>revoca del consenso quando il trattamento è basato sul consenso;</li>
              <li>tutela rispetto ai processi decisionali automatizzati nei casi previsti dalla legge.</li>
            </ul>
            <p>
              Fino alla pubblicazione dell’indirizzo privacy dedicato, le richieste potranno essere presentate
              attraverso i canali di contatto ufficiali pubblicati da TRAXION. Il Titolare potrà richiedere le
              informazioni ragionevolmente necessarie per verificare l’identità del richiedente.
            </p>
          </section>

          <section className={styles.section}>
            <h2>15. Reclamo all’autorità di controllo</h2>
            <p>
              L’utente ha diritto a presentare un reclamo all’autorità di controllo competente. Per un Titolare
              stabilito in Spagna, l’autorità di riferimento è l’Agencia Española de Protección de Datos (AEPD),
              fermo restando il diritto dell’interessato di rivolgersi all’autorità competente secondo la normativa
              applicabile.
            </p>
          </section>

          <section className={styles.section}>
            <h2>16. Minori</h2>
            <p>
              TRAXION non è progettato come servizio destinato a minori. Il servizio non intende raccogliere
              consapevolmente dati personali di minori attraverso funzionalità di trading o collegamento di account.
            </p>
          </section>

          <section className={styles.section}>
            <h2>17. Collegamenti verso servizi di terzi</h2>
            <p>
              Il sito può contenere collegamenti a servizi esterni. Quando l’utente lascia i domini TRAXION e
              utilizza un servizio di terzi, il relativo trattamento dei dati è disciplinato dalle condizioni e
              privacy policy del soggetto terzo.
            </p>
          </section>

          <section className={styles.section}>
            <h2>18. Modifiche alla Privacy Policy</h2>
            <p>
              La presente Privacy Policy potrà essere aggiornata per riflettere modifiche del servizio, cambiamenti
              dei provider, nuove funzionalità, modifiche dell’architettura o evoluzione normativa. La versione
              aggiornata sarà pubblicata su questa pagina con indicazione della data di aggiornamento.
            </p>
          </section>
        </div>

        <div className={styles.footer}>
          Ultimo aggiornamento: 3 settembre 2026. Questa versione contiene ancora dati identificativi del Titolare
          da completare prima della messa in esercizio commerciale definitiva.
        </div>
      </main>
    </div>
  );
}
