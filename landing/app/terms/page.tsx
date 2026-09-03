import type { Metadata } from "next";
import styles from "../privacy/privacy.module.css";

export const metadata: Metadata = {
  title: "Termini e Condizioni | TRAXION",
  description:
    "Termini e Condizioni TRAXION per accesso, trial, abbonamenti, uso di Hyperliquid, rischio operativo e diritti dei consumatori.",
  robots: { index: true, follow: true },
  alternates: {
    canonical: "https://traxion.lucianonovello.com/terms/",
    languages: {
      "it-IT": "https://traxion.lucianonovello.com/terms/",
      "es-ES": "https://traxion.lucianonovello.com/terms/es/",
    },
  },
};

export default function TermsAndConditionsPage() {
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.headerInner}>
          <a className={styles.brand} href="/">TRAXION</a>
          <a className={styles.back} href="/terms/es/">Español</a>
          <a className={styles.back} href="/">← Torna al sito</a>
        </div>
      </header>

      <main className={styles.main}>
        <div className={styles.hero}>
          <p className={styles.kicker}>Condizioni di utilizzo e abbonamento</p>
          <h1>Termini e Condizioni</h1>
          <p className={styles.lead}>
            I presenti Termini disciplinano l’accesso e l’utilizzo di TRAXION da parte di consumatori,
            professionisti e imprese, inclusi trial, piani a pagamento e funzionalità collegate a Hyperliquid.
          </p>
        </div>

        <div className={styles.notice}>
          <strong>Stato dei dati del Fornitore</strong>
          Dati identificativi completi del fornitore in corso di completamento. Prima della messa in esercizio
          commerciale definitiva saranno pubblicati denominazione fiscale, NIF/NIE, domicilio professionale e
          contatto contrattuale dedicato. I presenti Termini sono già predisposti per tale integrazione senza
          modificare le regole sostanziali del servizio.
        </div>

        <div className={styles.content}>
          <section className={styles.section}>
            <h2>1. Ambito e accettazione</h2>
            <p>
              I presenti Termini e Condizioni regolano l’accesso ai siti, alla web application e ai servizi
              tecnologici TRAXION, inclusi autenticazione wallet-native, configurazione del Risk Engine,
              collegamento di un Agent/API Wallet Hyperliquid, funzioni di analisi e processi di execution.
            </p>
            <p>
              Utilizzando il servizio o acquistando un piano, l’utente accetta i Termini nella versione resa
              disponibile prima della conclusione del contratto. Quando l’utente agisce come consumatore, restano
              applicabili tutti i diritti inderogabili previsti dalla normativa di tutela dei consumatori.
            </p>
          </section>

          <section className={styles.section}>
            <h2>2. Fornitore del servizio</h2>
            <p>
              Il Fornitore è il soggetto che gestisce TRAXION. Denominazione fiscale, NIF/NIE, domicilio
              professionale e contatto contrattuale sono in corso di completamento e saranno pubblicati in questa
              sezione prima della messa in esercizio commerciale definitiva.
            </p>
          </section>

          <section className={styles.section}>
            <h2>3. Definizioni essenziali</h2>
            <ul>
              <li><strong>TRAXION</strong>: il servizio tecnologico descritto nei presenti Termini.</li>
              <li><strong>Utente</strong>: la persona fisica o giuridica che accede o utilizza TRAXION.</li>
              <li><strong>Consumatore</strong>: la persona fisica che agisce per scopi estranei alla propria attività imprenditoriale o professionale.</li>
              <li><strong>Professionista</strong>: il soggetto che agisce nell’ambito della propria attività commerciale, imprenditoriale o professionale.</li>
              <li><strong>Account Hyperliquid</strong>: l’account esterno sul quale rimangono i fondi e le posizioni dell’utente.</li>
              <li><strong>Agent/API Wallet</strong>: credenziale dedicata autorizzata dall’utente per le operazioni tecnicamente consentite.</li>
              <li><strong>Risk Engine</strong>: insieme di regole deterministiche che limita o blocca determinate azioni operative.</li>
            </ul>
          </section>

          <section className={styles.section}>
            <h2>4. Requisiti di utilizzo</h2>
            <p>
              L’utente persona fisica deve avere almeno 18 anni e piena capacità di concludere il contratto. Chi
              utilizza TRAXION per conto di una società o altra organizzazione dichiara di avere il potere di
              vincolare tale soggetto ai presenti Termini.
            </p>
            <p>
              L’utente deve verificare che l’uso di TRAXION, Hyperliquid, asset digitali e perpetual sia consentito
              nella propria giurisdizione e deve rispettare le condizioni applicabili dei servizi terzi utilizzati.
              TRAXION può non essere disponibile in tutte le giurisdizioni.
            </p>
          </section>

          <section className={styles.section}>
            <h2>5. Natura del servizio</h2>
            <p>
              TRAXION è un sistema tecnologico che collega intelligence umana, componenti di Capital Intelligence
              AI, un Risk Engine e un execution layer. Il servizio può trasformare strategie e segnali strutturati
              in target operativi, verificare limiti di rischio, calcolare differenze rispetto allo stato reale
              dell’account e, quando l’utente ha attivato l’esecuzione, inviare operazioni autorizzate.
            </p>
            <p>
              TRAXION non promette rendimenti, risultati, disponibilità di mercato o protezione dalle perdite. Le
              informazioni e le funzionalità del servizio non sostituiscono una valutazione autonoma del rischio o
              eventuale consulenza professionale indipendente necessaria alla situazione dell’utente.
            </p>
          </section>

          <section className={styles.section}>
            <h2>6. Modello non-custodial</h2>
            <p>
              I fondi dell’utente rimangono sull’Account Hyperliquid dell’utente. TRAXION non riceve in custodia i
              fondi e non implementa funzioni di prelievo. Le operazioni previste dal servizio vengono effettuate
              mediante le autorizzazioni tecniche concesse all’Agent/API Wallet dedicato.
            </p>
            <p>
              La separazione tecnica tra wallet principale e Agent/API Wallet non elimina i rischi di trading,
              protocollo, exchange, rete o compromissione delle credenziali.
            </p>
          </section>

          <section className={styles.section}>
            <h2>7. Wallet principale e Agent/API Wallet</h2>
            <p>
              L’utente non deve mai comunicare a TRAXION seed phrase o chiave privata del wallet principale. Per
              l’execution deve essere utilizzato esclusivamente un Agent/API Wallet dedicato e autorizzato secondo
              le procedure Hyperliquid.
            </p>
            <p>
              L’utente è responsabile di custodire le proprie credenziali, verificare gli indirizzi collegati e
              revocare o sostituire l’Agent in caso di sospetta compromissione. La revoca dell’Agent interrompe la
              relativa autorità operativa ma non equivale automaticamente alla cancellazione di un abbonamento.
            </p>
          </section>

          <section className={styles.section}>
            <h2>8. Autenticazione e sicurezza dell’account</h2>
            <p>
              L’accesso può avvenire tramite firma crittografica del wallet e sessioni applicative. L’utente deve
              proteggere il dispositivo, il browser, il wallet e ogni altro strumento usato per accedere al servizio.
              I registri tecnici di autenticazione e attività possono essere utilizzati come elementi di verifica
              dei fatti, senza modificare le regole legali sull’onere della prova né i diritti inderogabili del
              consumatore.
            </p>
          </section>

          <section className={styles.section}>
            <h2>9. Risk Engine, SHADOW ed execution automatizzata</h2>
            <p>
              TRAXION può applicare limiti relativi, tra l’altro, a esposizione, leva, drawdown, perdita giornaliera,
              numero di posizioni, mercati consentiti e dimensionamento. I controlli possono consentire, ridurre,
              negare o rinviare un’azione.
            </p>
            <p>
              La modalità SHADOW consente di calcolare target, sizing e controlli senza inviare ordini. Quando
              l’utente attiva l’esecuzione reale, autorizza il servizio a inviare operazioni compatibili con la
              configurazione attiva e con i limiti tecnici applicabili.
            </p>
          </section>

          <section className={styles.section}>
            <h2>10. Funzionalità di intelligenza artificiale</h2>
            <p>
              Le componenti AI possono analizzare dati e produrre informazioni strutturate, priorità o valutazioni
              di contesto. Possono commettere errori, produrre risultati incompleti o non riflettere eventi di
              mercato sopravvenuti.
            </p>
            <p>
              L’AI non costituisce direttamente l’autorità tecnica finale di firma degli ordini. L’execution resta
              separata e sottoposta alle regole deterministiche e alle autorizzazioni previste dall’architettura del
              servizio.
            </p>
          </section>

          <section className={styles.section}>
            <h2>11. Obblighi dell’utente</h2>
            <p>L’utente si impegna a:</p>
            <ul>
              <li>fornire dati e configurazioni corretti quando necessari al servizio;</li>
              <li>proteggere wallet, dispositivi e Agent/API Wallet;</li>
              <li>controllare periodicamente stato dell’account, posizioni, limiti e autorizzazioni;</li>
              <li>mantenere un livello di rischio coerente con la propria situazione e capacità di perdita;</li>
              <li>rispettare la legge applicabile e le condizioni dei provider terzi;</li>
              <li>segnalare tempestivamente anomalie o sospette compromissioni attraverso il canale di assistenza disponibile.</li>
            </ul>
          </section>

          <section className={styles.section}>
            <h2>12. Usi vietati</h2>
            <p>È vietato utilizzare TRAXION per:</p>
            <ul>
              <li>attività illegali, fraudolente o abusive;</li>
              <li>aggirare controlli di sicurezza, limiti operativi o restrizioni di accesso;</li>
              <li>interferire con il funzionamento del servizio o tentare accessi non autorizzati;</li>
              <li>utilizzare credenziali o account di terzi senza autorizzazione;</li>
              <li>copiarne o sfruttarne software, contenuti o sistemi oltre quanto consentito dalla legge o dai presenti Termini.</li>
            </ul>
          </section>

          <section className={styles.section}>
            <h2>13. Trial gratuito</h2>
            <p>
              La configurazione corrente prevede un trial di 14 giorni, attivato al primo accesso e senza carta di
              pagamento. Durante il trial possono essere applicati limiti specifici di portafoglio, posizioni,
              notional per operazione, moltiplicatore o altre funzionalità, mostrati nella web application e nella
              pagina commerciale.
            </p>
            <p>
              Il trial è destinato alla valutazione del servizio e può essere limitato a una singola attivazione per
              utente o wallet, salvo diversa promozione espressamente indicata.
            </p>
          </section>

          <section className={styles.section}>
            <h2>14. Piani a pagamento e prezzi</h2>
            <p>
              TRAXION può offrire piani mensili e annuali con limiti e funzionalità differenti. Prezzi, valuta,
              periodicità, eventuali sconti personali, capacità del piano ed eventuali condizioni aggiuntive sono
              quelli mostrati all’utente prima della conferma dell’acquisto.
            </p>
            <p>
              In caso di aggiornamento del catalogo, gli importi mostrati nel checkout sono quelli applicabili alla
              sottoscrizione che l’utente sta per confermare. Eventuali imposte applicabili sono gestite secondo la
              normativa e le informazioni rese disponibili nel processo di pagamento.
            </p>
          </section>

          <section className={styles.section}>
            <h2>15. Pagamenti tramite Stripe</h2>
            <p>
              I pagamenti e gli abbonamenti possono essere gestiti tramite Stripe. TRAXION non deve memorizzare i
              dati completi della carta. L’utente può essere reindirizzato alle interfacce Stripe per completare il
              pagamento o gestire l’abbonamento.
            </p>
            <p>
              L’attivazione delle funzionalità a pagamento dipende dalla conferma tecnica dello stato
              dell’abbonamento. Un pagamento rifiutato, scaduto o in stato irregolare può comportare limitazione o
              sospensione delle funzionalità a pagamento fino alla regolarizzazione.
            </p>
          </section>

          <section className={styles.section}>
            <h2>16. Rinnovo e cancellazione dell’abbonamento</h2>
            <p>
              Salvo diversa indicazione nel checkout, i piani a pagamento configurati come abbonamenti ricorrenti si
              rinnovano per periodi successivi della stessa periodicità finché non vengono cancellati. La gestione
              dell’abbonamento avviene tramite il billing portal messo a disposizione dal servizio e da Stripe.
            </p>
            <p>
              La data effettiva di cessazione, l’eventuale accesso residuo fino alla fine del periodo già pagato e
              ogni conseguenza economica della cancellazione sono quelle mostrate nel portale e determinate dalla
              configurazione applicabile, fatti salvi i diritti inderogabili del consumatore.
            </p>
          </section>

          <section className={styles.section}>
            <h2>17. Diritto di recesso dei consumatori</h2>
            <p>
              Il consumatore che conclude a distanza un contratto a pagamento dispone, salvo le eccezioni previste
              dalla legge, di 14 giorni naturali dalla conclusione del contratto per esercitare il diritto di
              recesso senza dover indicare una motivazione.
            </p>
            <p>
              Se il consumatore richiede espressamente che la prestazione inizi durante il periodo di recesso e
              successivamente recede, può essere dovuto l’importo proporzionale al servizio effettivamente fornito
              fino alla comunicazione del recesso, quando ricorrono le condizioni previste dalla normativa
              applicabile. TRAXION non considera il semplice utilizzo del servizio come rinuncia automatica al
              diritto di recesso.
            </p>
            <p>
              Il diritto può cessare nei casi consentiti dalla legge quando il servizio sia stato completamente
              eseguito dopo previo consenso espresso del consumatore e sua espressa presa d’atto della perdita del
              diritto. Qualsiasi consenso o presa d’atto necessario deve essere raccolto separatamente nel processo
              di acquisto.
            </p>
            <p>
              Il canale dedicato per l’esercizio del recesso è in corso di completamento e sarà pubblicato prima
              della messa in esercizio commerciale definitiva. Una volta pubblicati i dati del Fornitore, resta
              possibile utilizzare qualsiasi dichiarazione inequivoca ammessa dalla legge.
            </p>
            <h3>Modulo tipo di recesso</h3>
            <p>
              Il consumatore può utilizzare il seguente modello per comunicare il recesso. Non è obbligatorio usare
              esattamente questo modulo quando la dichiarazione trasmessa è comunque inequivoca.
            </p>
            <p><strong>Destinatario:</strong> Fornitore di TRAXION — dati identificativi, domicilio professionale e canale di contatto in corso di completamento.</p>
            <p>
              Con la presente comunico/comunichiamo il recesso dal mio/nostro contratto di prestazione del servizio
              TRAXION concluso a distanza.
            </p>
            <ul>
              <li>Data di conclusione del contratto: ____________________</li>
              <li>Nome del/dei consumatore/i: ____________________</li>
              <li>Indirizzo del/dei consumatore/i: ____________________</li>
              <li>Wallet o identificativo account, se necessario per individuare il contratto: ____________________</li>
              <li>Data della comunicazione: ____________________</li>
              <li>Firma del/dei consumatore/i, solo se il modulo è presentato su carta: ____________________</li>
            </ul>
          </section>

          <section className={styles.section}>
            <h2>18. Rimborsi</h2>
            <p>
              I rimborsi dovuti in conseguenza di un valido esercizio del diritto di recesso o di altri diritti
              inderogabili sono effettuati secondo la normativa applicabile. Al di fuori di tali casi e di eventuali
              condizioni promozionali espresse, i pagamenti relativi a periodi di servizio già iniziati non danno
              automaticamente diritto a rimborso.
            </p>
          </section>

          <section className={styles.section}>
            <h2>19. Diritti del consumatore sui servizi digitali</h2>
            <p>
              Nulla nei presenti Termini limita i diritti inderogabili del consumatore relativi alla conformità,
              fornitura e rimedi previsti dalla normativa applicabile ai servizi digitali. In caso di mancata
              conformità imputabile al servizio, il consumatore conserva i rimedi previsti dalla legge.
            </p>
          </section>

          <section className={styles.section}>
            <h2>20. Rischi di trading e perdita del capitale</h2>
            <p>
              Il trading di asset digitali e perpetual comporta un rischio elevato. Leva, volatilità, liquidazioni,
              slippage, variazioni di liquidità, movimenti rapidi del mercato, funding, errori di prezzo, latenza o
              altri eventi possono produrre perdite significative fino alla perdita parziale o totale del capitale
              destinato all’operatività.
            </p>
            <p>
              Controlli di rischio, automazione e riconciliazione riducono alcune categorie di errore operativo ma
              non eliminano il rischio di mercato né garantiscono che un ordine venga eseguito al prezzo, nella
              quantità o nel momento desiderato.
            </p>
          </section>

          <section className={styles.section}>
            <h2>21. Rischi di Hyperliquid e servizi terzi</h2>
            <p>
              TRAXION dipende da infrastrutture e servizi esterni, tra cui Hyperliquid, blockchain, reti, wallet,
              provider RPC o API, Stripe, infrastruttura hosting e, per alcune funzioni, provider AI. Interruzioni,
              modifiche, limitazioni, errori o indisponibilità di tali soggetti possono influire sul servizio.
            </p>
            <p>
              TRAXION è un progetto indipendente e non è affiliato, approvato o sponsorizzato da Hyperliquid. L’uso
              di servizi terzi resta soggetto anche ai rispettivi termini e condizioni.
            </p>
          </section>

          <section className={styles.section}>
            <h2>22. Disponibilità, manutenzione e modifiche tecniche</h2>
            <p>
              Il Fornitore può eseguire manutenzione, aggiornamenti, correzioni di sicurezza e modifiche necessarie
              all’evoluzione del servizio. Non è garantita un’operatività ininterrotta o priva di errori.
            </p>
            <p>
              Quando una modifica incide materialmente su un abbonamento a pagamento o su diritti del consumatore,
              saranno rispettati gli obblighi di informazione, continuità e rimedio previsti dalla normativa
              applicabile.
            </p>
          </section>

          <section className={styles.section}>
            <h2>23. Sospensione e limitazione del servizio</h2>
            <p>
              TRAXION può sospendere o limitare l’accesso quando ciò sia ragionevolmente necessario per sicurezza,
              manutenzione, prevenzione di abusi, violazioni dei presenti Termini, richieste legali, problemi di
              pagamento o tutela dell’integrità della piattaforma.
            </p>
            <p>
              Ove ragionevolmente possibile e compatibile con esigenze urgenti di sicurezza, l’utente sarà
              informato della sospensione e delle modalità per rimuoverne la causa.
            </p>
          </section>

          <section className={styles.section}>
            <h2>24. Limitazione di responsabilità</h2>
            <p>
              TRAXION risponde delle proprie obbligazioni nei limiti previsti dalla legge applicabile. Nessuna
              clausola dei presenti Termini esclude o limita responsabilità che non possa essere validamente esclusa
              o limitata, né riduce i diritti inderogabili riconosciuti ai consumatori.
            </p>
            <p>
              Nei rapporti con professionisti, e nei limiti consentiti dalla legge, il Fornitore non risponde di
              perdite indirette o consequenziali, lucro cessante, perdita di opportunità o danni derivanti
              esclusivamente da decisioni di trading dell’utente, condizioni di mercato o malfunzionamenti di
              servizi terzi fuori dal ragionevole controllo del Fornitore.
            </p>
            <p>
              Le presenti limitazioni non si applicano quando la legge vieti di limitarle, inclusi i casi di dolo o
              altre ipotesi di responsabilità inderogabile previste dall’ordinamento applicabile.
            </p>
          </section>

          <section className={styles.section}>
            <h2>25. Proprietà intellettuale</h2>
            <p>
              Software, interfacce, marchi, testi, grafica, architetture, documentazione e contenuti originali di
              TRAXION sono protetti dalla normativa applicabile e restano di proprietà dei rispettivi titolari.
              All’utente viene concessa una licenza personale, limitata, non esclusiva e revocabile per utilizzare
              il servizio secondo i presenti Termini.
            </p>
          </section>

          <section className={styles.section}>
            <h2>26. Privacy e dati personali</h2>
            <p>
              Il trattamento dei dati personali è descritto nella <a href="/privacy/">Privacy Policy TRAXION</a>,
              che costituisce l’informativa dedicata su dati trattati, finalità, basi giuridiche, sicurezza,
              destinatari, conservazione e diritti degli interessati.
            </p>
          </section>

          <section className={styles.section}>
            <h2>27. Contrattazione elettronica</h2>
            <p>
              Prima dell’acquisto l’utente deve poter consultare, memorizzare e riprodurre i presenti Termini. Il
              flusso di acquisto mostra il piano, la periodicità e il prezzo applicabile prima della conferma del
              pagamento.
            </p>
            <p>
              I dati inseriti nel checkout possono essere verificati e corretti attraverso i controlli messi a
              disposizione dall’interfaccia di pagamento prima della conferma. La versione dei Termini accettata e
              gli eventi contrattuali possono essere conservati elettronicamente per finalità operative, probatorie
              e legali.
            </p>
            <p>
              A conclusione del procedimento deve essere fornita una conferma elettronica dell’acquisto o
              dell’attivazione. La lingua contrattuale è quella della versione dei Termini resa disponibile e
              accettata nel relativo processo di acquisto. La versione in castigliano resta disponibile per i
              consumatori che contrattano in Spagna.
            </p>
          </section>

          <section className={styles.section}>
            <h2>28. Modifiche ai Termini, ai piani e ai prezzi</h2>
            <p>
              Il Fornitore può aggiornare i presenti Termini per ragioni legali, di sicurezza, tecniche o
              commerciali. Le modifiche materiali applicabili a contratti in corso saranno comunicate con il
              preavviso richiesto dalla legge e non pregiudicheranno i diritti inderogabili già maturati.
            </p>
            <p>
              Modifiche di prezzo per rinnovi futuri saranno applicate secondo le informazioni comunicate prima del
              relativo rinnovo e nel rispetto della normativa applicabile.
            </p>
          </section>

          <section className={styles.section}>
            <h2>29. Legge applicabile e controversie</h2>
            <p>
              I presenti Termini sono disciplinati dalla legge spagnola, fatti salvi i diritti e le norme imperative
              che proteggono il consumatore nel paese della sua residenza abituale quando applicabili.
            </p>
            <p>
              Per i consumatori, la competenza territoriale resta quella stabilita dalle norme imperative
              applicabili e i presenti Termini non impongono un foro diverso da quello legalmente protetto. Per i
              professionisti, salvo diversa norma inderogabile, l’eventuale foro convenzionale sarà indicato insieme
              ai dati legali definitivi del Fornitore.
            </p>
          </section>

          <section className={styles.section}>
            <h2>30. Reclami e risoluzione delle controversie</h2>
            <p>
              L’utente è invitato a contattare il Fornitore per consentire una gestione diretta di eventuali
              contestazioni. I consumatori conservano il diritto di rivolgersi alle autorità e agli organismi di
              risoluzione alternativa delle controversie competenti quando previsti dalla normativa applicabile.
            </p>
            <p>
              Il canale contrattuale dedicato è in corso di completamento e sarà pubblicato prima della messa in
              esercizio commerciale definitiva.
            </p>
          </section>

          <section className={styles.section}>
            <h2>31. Clausole finali</h2>
            <p>
              Se una disposizione dei presenti Termini viene dichiarata invalida o inefficace, le restanti
              disposizioni continuano ad applicarsi nella misura consentita dalla legge. Il mancato esercizio di un
              diritto non costituisce rinuncia allo stesso.
            </p>
            <p>
              L’utente non può trasferire il proprio account o le proprie credenziali a terzi in violazione dei
              presenti Termini. Eventuali trasferimenti del contratto da parte del Fornitore saranno effettuati nel
              rispetto della normativa applicabile e senza ridurre i diritti inderogabili del consumatore.
            </p>
          </section>
        </div>

        <div className={styles.footer}>
          Ultimo aggiornamento: 3 settembre 2026. I dati identificativi e il contatto del Fornitore saranno
          completati prima della messa in esercizio commerciale definitiva.
        </div>
      </main>
    </div>
  );
}
