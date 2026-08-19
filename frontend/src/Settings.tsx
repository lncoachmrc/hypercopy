import {useEffect,useState} from 'react';
import {ACTIVATION_TIMEOUT_MS,del,get,post,put} from './api';
import {useAuth} from './auth';
import './settings-state.css';

type TradingNetwork='testnet'|'mainnet';
type NetworkSwitchBlocker={code:'pause'|'positions'|'jobs'|'executions';message:string};
type NetworkDialog={target:TradingNetwork;kind:'blocked'|'confirm-mainnet'}|null;
type Me={
  auth_wallet:string;
  role:string;
  copy_state:string;
  risk_state:string;
  master_network:string;
  follower_network:TradingNetwork;
  network_started_at:string;
  network_switch_ready:boolean;
  network_switch_blockers:NetworkSwitchBlocker[];
  trading_account:null|{
    account_address:string;
    network:string;
    agent_address:string;
    agent_name:string;
    credential_status:string;
    expires_at:string|null;
  };
};

type Risk={
  multiplier:string;
  max_notional_per_trade:string;
  max_total_exposure:string;
  max_asset_exposure:string;
  max_leverage:string;
  max_positions:number;
  max_drawdown_pct:string;
  max_daily_loss_pct:string;
  min_notional:string;
  max_slippage_bps:number;
  close_only:boolean;
  allow_assets:string[];
  block_assets:string[];
};

type RiskMode='basic'|'pro';
type BasicProfile='prudent'|'balanced'|'faithful'|'custom';
type BasicMarkets='all'|'majors'|'btc'|'custom';

type BasicProfileConfig={
  title:string;
  badge?:string;
  description:string;
  multiplier:number;
  maxLeverage:number;
  maxPositions:number;
  maxDrawdown:number;
  dailyLoss:number;
  exposureFactor:number;
  assetShare:number;
};

const MAJOR_ASSETS=['BTC','ETH','SOL','XRP','BNB','ADA','AVAX','LINK','LTC','BCH'];

const BASIC_PROFILES:Record<Exclude<BasicProfile,'custom'>,BasicProfileConfig>={
  prudent:{
    title:'Prudente',
    description:'Utilizza il 50% dell’esposizione strategica e limita maggiormente leva e capitale impiegato.',
    multiplier:0.5,
    maxLeverage:3,
    maxPositions:10,
    maxDrawdown:10,
    dailyLoss:5,
    exposureFactor:1,
    assetShare:0.15,
  },
  balanced:{
    title:'Bilanciato',
    badge:'Consigliato',
    description:'Equilibrio tra adesione alla strategia ibrida e protezione del capitale.',
    multiplier:0.75,
    maxLeverage:5,
    maxPositions:20,
    maxDrawdown:15,
    dailyLoss:7.5,
    exposureFactor:1.5,
    assetShare:0.20,
  },
  faithful:{
    title:'Strategia completa',
    description:'Utilizza il 100% dell’esposizione proporzionale prevista dalla strategia e lascia più spazio alla leva operativa.',
    multiplier:1,
    maxLeverage:40,
    maxPositions:50,
    maxDrawdown:20,
    dailyLoss:10,
    exposureFactor:3,
    assetShare:0.25,
  },
};

export default function Settings(){
  const {refresh}=useAuth();
  const [me,setMe]=useState<Me|null>(null);
  const [risk,setRisk]=useState<Risk|null>(null);
  const [equity,setEquity]=useState<number|null>(null);
  const [agent,setAgent]=useState('');
  const [key,setKey]=useState('');
  const [msg,setMsg]=useState('');
  const [networkDialog,setNetworkDialog]=useState<NetworkDialog>(null);
  const [riskMode,setRiskMode]=useState<RiskMode>(()=>localStorage.getItem('hypercopy:risk-mode')==='pro'?'pro':'basic');
  const [basicProfile,setBasicProfile]=useState<BasicProfile>('custom');
  const [basicMarkets,setBasicMarkets]=useState<BasicMarkets>('custom');

  const load=async()=>{
    const [meValue,riskValue,dashboard]=await Promise.all([
      get<Me>('/me'),
      get<Risk>('/risk-profile'),
      get<{equity:number|null}>('/dashboard'),
    ]);
    setMe(meValue);
    setRisk(riskValue);
    setEquity(dashboard.equity);
    setBasicProfile(inferBasicProfile(riskValue));
    setBasicMarkets(inferBasicMarkets(riskValue));
  };

  const refreshNetworkState=async()=>{
    try{setMe(await get<Me>('/me'))}catch{/* session errors are handled by the normal app flow */}
  };

  useEffect(()=>{
    void load();
    const timer=setInterval(()=>void refreshNetworkState(),2500);
    return()=>clearInterval(timer);
  },[]);

  useEffect(()=>{
    if(networkDialog?.kind==='blocked'&&me?.network_switch_ready)setNetworkDialog(null);
  },[me?.network_switch_ready,networkDialog?.kind]);

  const switchRiskMode=(mode:RiskMode)=>{
    setRiskMode(mode);
    localStorage.setItem('hypercopy:risk-mode',mode);
  };

  const performTradingNetworkSwitch=async(network:TradingNetwork)=>{
    if(!me||me.follower_network===network)return;
    const label=network.toUpperCase();
    setNetworkDialog(null);
    setMsg(`Passaggio a ${label} in corso…`);
    try{
      const updated=await put<Me>('/trading-network',{network});
      setMe(updated);
      setAgent('');
      setKey('');
      setEquity(null);
      await load();
      setMsg(`Rete ${label} selezionata. Configura ora l’API Wallet ${label}.`);
    }catch(e){
      await refreshNetworkState();
      setMsg(e instanceof Error?e.message:'Cambio rete non riuscito');
    }
  };

  const requestTradingNetworkSwitch=(network:TradingNetwork)=>{
    if(!me||me.follower_network===network)return;
    if(!me.network_switch_ready){
      setNetworkDialog({target:network,kind:'blocked'});
      return;
    }
    if(network==='mainnet'){
      setNetworkDialog({target:network,kind:'confirm-mainnet'});
      return;
    }
    void performTradingNetworkSwitch(network);
  };

  const link=async()=>{
    setMsg(`Verifica API Wallet ${me?.follower_network?.toUpperCase()||''}…`);
    try{
      await post('/trading-account',{agent_address:agent,agent_private_key:key});
      setKey('');
      setMsg(`API Wallet ${me?.follower_network?.toUpperCase()||''} verificato e chiave cifrata.`);
      await load();
    }catch(e){
      setMsg(e instanceof Error?e.message:'Errore');
    }
  };

  const save=async(next?:Risk)=>{
    const value=next||risk;
    if(!value)return;
    const stored=await put('/risk-profile',value) as Risk;
    setRisk(stored);
    setMsg('Profilo rischio salvato.');
  };

  const applyBasic=async()=>{
    if(!risk)return;
    if(basicProfile==='custom'){
      setMsg('La configurazione attuale è personalizzata. Scegli Prudente, Bilanciato o Strategia completa per applicare la modalità Basic.');
      return;
    }
    const next=buildBasicRisk(risk,basicProfile,basicMarkets,equity);
    const stored=await put('/risk-profile',next) as Risk;
    setRisk(stored);
    setBasicProfile(inferBasicProfile(stored));
    setBasicMarkets(inferBasicMarkets(stored));
    setMsg(`Profilo Basic “${BASIC_PROFILES[basicProfile].title}” applicato. Lo stato operativo non è stato modificato.`);
  };

  const presetBtc=async()=>{
    if(!risk)return;
    const next:Risk={
      ...risk,
      multiplier:'1',
      max_notional_per_trade:'25',
      max_total_exposure:'25',
      max_asset_exposure:'25',
      max_leverage:'2',
      max_positions:1,
      min_notional:'10',
      allow_assets:['BTC'],
      block_assets:[],
      close_only:false,
    };
    await save(next);
    setMsg('Preset TESTNET BTC pronto: 1 posizione, solo BTC, max $25, leva massima 2×. La strategia non viene attivata automaticamente.');
  };

  const presetMulti=async()=>{
    if(!risk)return;
    const next:Risk={
      ...risk,
      multiplier:'1',
      max_notional_per_trade:'100',
      max_total_exposure:'1000',
      max_asset_exposure:'100',
      max_leverage:'40',
      max_positions:50,
      min_notional:'10',
      allow_assets:[],
      block_assets:[],
      close_only:false,
    };
    await save(next);
    setMsg('Preset TESTNET multi-asset pronto: tutti i mercati disponibili, max 50 posizioni, $100 per asset/trade, $1000 esposizione totale. Leva allineata alla strategia sorgente fino a 40× e al limite del singolo mercato. La strategia non viene attivata automaticamente.');
  };

  const setShadow=async()=>{
    await post('/copy/shadow');
    await load();
    await refresh();
    setMsg('Modalità SHADOW attiva: target e controlli vengono calcolati ma non vengono inviati ordini.');
  };

  const activate=async()=>{
    const network=me?.follower_network?.toUpperCase()||'';
    if(!confirm(`Attivare il trading automatizzato della strategia ibrida sulla rete ${network}?${network==='MAINNET'?' Verranno utilizzati fondi reali.':''}`))return;
    setMsg(`Attivazione ${network} in corso…`);
    try{
      await post('/copy/resume',undefined,ACTIVATION_TIMEOUT_MS);
      await load();
      await refresh();
      setMsg(`Strategia ${network} attiva.`);
    }catch(e){
      setMsg(e instanceof Error?e.message:'Errore attivazione');
    }
  };

  const closeAll=async()=>{
    if(!confirm('Chiudere tutte le posizioni TRAXION gestite? La strategia verrà messa automaticamente in PAUSA per evitare riaperture.'))return;
    try{
      const result=await post<{queued:number;paused:boolean;deferred_enqueue:number}>('/copy/close-positions',{confirmation:'CLOSE',reason:'User requested close all'});
      await load(); await refresh();
      setMsg(result.queued>0 ? `Strategia in PAUSA. ${result.queued} chiusure reduce-only accodate.` : 'Strategia in PAUSA. Nessuna posizione TRAXION gestita da chiudere.');
    }catch(e){setMsg(e instanceof Error?e.message:'Chiusura posizioni non riuscita')}
  };

  const canChangeNetwork=Boolean(me?.network_switch_ready);

  return <>
    <div className="title">
      <div>
        <h1>Configurazione</h1>
        <p>Il wallet con cui accedi a TRAXION è anche il tuo account operativo Hyperliquid. Per autorizzare gli ordini usa soltanto un API Wallet Hyperliquid dedicato e revocabile.</p>
      </div>
    </div>

    <div className="cols">
      <section className="panel">
        <h2>Account operativo</h2>
        {me&&<dl>
          <dt>Rete strategia</dt><dd>{me.master_network.toUpperCase()}</dd>
          <dt>Rete account</dt><dd>{me.follower_network.toUpperCase()}</dd>
          <dt>Modalità operativa</dt><dd>{me.copy_state}</dd>
        </dl>}

        {me&&<div className={`network-selector ${me.follower_network==='mainnet'?'mainnet':''}`}>
          <div className="network-selector-copy">
            <strong>Rete operativa Hyperliquid</strong>
            <span>Passa da TESTNET a MAINNET e viceversa direttamente da qui. TRAXION controlla automaticamente quando il cambio è sicuro.</span>
          </div>
          <div className={`network-toggle ${canChangeNetwork?'ready':'locked'}`} role="group" aria-label="Rete operativa Hyperliquid">
            <button className={`${me.follower_network==='testnet'?'active testnet':''} ${me.follower_network!=='testnet'&&!canChangeNetwork?'locked':''}`} aria-pressed={me.follower_network==='testnet'} aria-disabled={me.follower_network!=='testnet'&&!canChangeNetwork} disabled={me.follower_network==='testnet'} onClick={()=>requestTradingNetworkSwitch('testnet')}>TESTNET</button>
            <button className={`${me.follower_network==='mainnet'?'active mainnet':''} ${me.follower_network!=='mainnet'&&!canChangeNetwork?'locked':''}`} aria-pressed={me.follower_network==='mainnet'} aria-disabled={me.follower_network!=='mainnet'&&!canChangeNetwork} disabled={me.follower_network==='mainnet'} onClick={()=>requestTradingNetworkSwitch('mainnet')}>MAINNET</button>
          </div>
          <p className="network-help">Il toggle si sblocca automaticamente quando la strategia è in PAUSA, le posizioni gestite sono chiuse, la coda è vuota e non esistono esecuzioni SUBMITTING/UNKNOWN. Al cambio rete TRAXION rimuove automaticamente la vecchia credenziale e mostra i campi dell’API Wallet della nuova rete.</p>
          {me.follower_network==='mainnet'&&<div className="network-mainnet-warning"><strong>MAINNET selezionata</strong><span>Questa rete utilizza fondi reali. Configura esclusivamente un API Wallet Hyperliquid dedicato e revocabile.</span></div>}
        </div>}

        {me?.trading_account?<>
          <dl>
            <dt>Wallet / account operativo</dt><dd>{me.trading_account.account_address}</dd>
            <dt>Rete account operativo</dt><dd>{me.trading_account.network.toUpperCase()}</dd>
            <dt>API Wallet / Agent</dt><dd>{me.trading_account.agent_address}</dd>
            <dt>Stato credenziale</dt><dd>{me.trading_account.credential_status}</dd>
            <dt>Scadenza</dt><dd>{me.trading_account.expires_at?new Date(me.trading_account.expires_at).toLocaleDateString():'—'}</dd>
          </dl>
          <p className="muted">La private key del wallet principale non viene mai richiesta da TRAXION.</p>
          <button className="danger" onClick={async()=>{await del('/trading-account');await load();setMsg('API Wallet scollegato.')}}>Scollega API Wallet</button>
        </>:<>
          <div className={`network-credential-card ${me?.follower_network==='mainnet'?'mainnet':''}`}>
            <div className="network-credential-title">
              <strong>Configura API Wallet {me?.follower_network?.toUpperCase()}</strong>
              <span>{me?.follower_network==='mainnet'?'Credenziale operativa per fondi reali.':'Credenziale operativa per l’ambiente di prova.'}</span>
            </div>
            <dl><dt>Wallet / account operativo</dt><dd>{me?.auth_wallet||'Caricamento…'}</dd></dl>
            <p className="muted">Questo indirizzo deriva dal wallet con cui hai effettuato l'accesso e non può essere sostituito con un altro account.</p>
            <label>API Wallet Address · {me?.follower_network?.toUpperCase()}<input value={agent} onChange={e=>setAgent(e.target.value)} placeholder="0x…" autoComplete="off"/></label>
            <label>API Wallet Private Key · {me?.follower_network?.toUpperCase()}<input type="password" autoComplete="off" value={key} onChange={e=>setKey(e.target.value)} placeholder="0x…"/></label>
            <p className="muted">Inserisci indirizzo e private key dello stesso API Wallet Hyperliquid autorizzato sulla rete {me?.follower_network?.toUpperCase()}. TRAXION verifica che la chiave generi esattamente quell'indirizzo e che l'Agent sia autorizzato sul tuo account.</p>
            <button className="primary" onClick={()=>void link()} disabled={!agent||!key}>Verifica e collega</button>
          </div>
        </>}

        <hr/>
        <div className="actions">
          <button className={`state-button state-button--pause ${me?.copy_state==='PAUSED'?'active':''}`} aria-pressed={me?.copy_state==='PAUSED'} onClick={async()=>{await post('/copy/pause');await load();await refresh()}} disabled={me?.copy_state==='PAUSED'}>Pausa</button>
          <button className={`state-button state-button--shadow ${me?.copy_state==='SHADOW'?'active':''}`} aria-pressed={me?.copy_state==='SHADOW'} onClick={()=>void setShadow()} disabled={!me?.trading_account||me?.copy_state==='SHADOW'}>Modalità SHADOW</button>
          <button className={`state-button state-button--active ${me?.copy_state==='ACTIVE'?'active':''}`} aria-pressed={me?.copy_state==='ACTIVE'} onClick={()=>void activate()} disabled={!me?.trading_account||me?.copy_state==='ACTIVE'}>Attiva strategia</button>
          <button className="danger" onClick={()=>void closeAll()}>Chiudi posizioni</button>
        </div>
        <p className="muted">SHADOW calcola target, sizing e controlli senza inviare ordini. “Attiva strategia” abilita l'esecuzione automatizzata sulla rete operativa selezionata.</p>
      </section>

      <section className="panel risk-panel">
        <div className="risk-heading">
          <div>
            <h2>Risk Engine</h2>
            <p className="muted">Scegli una configurazione guidata oppure passa ai controlli avanzati.</p>
          </div>
          <div className="risk-mode-toggle" aria-label="Modalità Risk Engine">
            <button className={riskMode==='basic'?'active':''} onClick={()=>switchRiskMode('basic')}>Basic</button>
            <button className={riskMode==='pro'?'active':''} onClick={()=>switchRiskMode('pro')}>Pro</button>
          </div>
        </div>

        {risk&&riskMode==='basic'&&<BasicRisk
          risk={risk}
          equity={equity}
          profile={basicProfile}
          markets={basicMarkets}
          setProfile={setBasicProfile}
          setMarkets={setBasicMarkets}
          apply={()=>void applyBasic()}
          openPro={()=>switchRiskMode('pro')}
        />}

        {risk&&riskMode==='pro'&&<>
          <div className="pro-warning">Modalità Pro: controllo completo dei limiti. Le modifiche possono cambiare direttamente sizing, leva consentita e comportamento dell'esecuzione automatizzata.</div>
          <div className="formgrid">
            <Num label="Multiplier" value={risk.multiplier} set={v=>setRisk({...risk,multiplier:v})}/>
            <Num label="Max / trade $" value={risk.max_notional_per_trade} set={v=>setRisk({...risk,max_notional_per_trade:v})}/>
            <Num label="Max exposure $" value={risk.max_total_exposure} set={v=>setRisk({...risk,max_total_exposure:v})}/>
            <Num label="Max asset $" value={risk.max_asset_exposure} set={v=>setRisk({...risk,max_asset_exposure:v})}/>
            <Num label="Max leverage" value={risk.max_leverage} set={v=>setRisk({...risk,max_leverage:v})}/>
            <Num label="Max positions" value={String(risk.max_positions)} set={v=>setRisk({...risk,max_positions:Math.max(1,Number(v)||1)})}/>
            <Num label="Min notional $" value={risk.min_notional} set={v=>setRisk({...risk,min_notional:v})}/>
            <Num label="Max drawdown %" value={risk.max_drawdown_pct} set={v=>setRisk({...risk,max_drawdown_pct:v})}/>
            <Num label="Daily loss %" value={risk.max_daily_loss_pct} set={v=>setRisk({...risk,max_daily_loss_pct:v})}/>
            <Num label="Slippage bps" value={String(risk.max_slippage_bps)} set={v=>setRisk({...risk,max_slippage_bps:Number(v)})}/>
            <label>Asset consentiti (CSV)<input value={risk.allow_assets.join(', ')} onChange={e=>setRisk({...risk,allow_assets:e.target.value.split(',').map(x=>x.trim().toUpperCase()).filter(Boolean)})} placeholder="vuoto = tutti"/></label>
            <label>Asset bloccati (CSV)<input value={risk.block_assets.join(', ')} onChange={e=>setRisk({...risk,block_assets:e.target.value.split(',').map(x=>x.trim().toUpperCase()).filter(Boolean)})} placeholder="es. BTC, ETH"/></label>
            <label className="check"><input type="checkbox" checked={risk.close_only} onChange={e=>setRisk({...risk,close_only:e.target.checked})}/> Close-only</label>
            {me?.follower_network==='testnet'&&<button onClick={()=>void presetBtc()}>Preset test BTC sicuro</button>}
            {me?.follower_network==='testnet'&&me?.role==='SUPERADMIN'&&<button onClick={()=>void presetMulti()}>Preset test multi-asset TESTNET</button>}
            <button className="primary" onClick={()=>void save()}>Salva limiti</button>
          </div>
        </>}
      </section>
    </div>
    {networkDialog&&me&&<NetworkSwitchDialog
      me={me}
      target={networkDialog.target}
      kind={networkDialog.kind}
      close={()=>setNetworkDialog(null)}
      refreshState={()=>void refreshNetworkState()}
      confirm={()=>void performTradingNetworkSwitch(networkDialog.target)}
    />}
    {msg&&<div className="toast">{msg}</div>}
  </>;
}

function NetworkSwitchDialog({me,target,kind,close,refreshState,confirm}:{
  me:Me;
  target:TradingNetwork;
  kind:'blocked'|'confirm-mainnet';
  close:()=>void;
  refreshState:()=>void;
  confirm:()=>void;
}){
  const blocked=new Set(me.network_switch_blockers.map(x=>x.code));
  const targetLabel=target.toUpperCase();
  const requirements:[NetworkSwitchBlocker['code'],string][]=[
    ['pause','Strategia in PAUSA'],
    ['positions','Tutte le posizioni TRAXION chiuse'],
    ['jobs','Nessun job QUEUED / PROCESSING / RETRYING'],
    ['executions','Nessuna esecuzione SUBMITTING / UNKNOWN'],
  ];
  if(kind==='confirm-mainnet')return <div className="network-modal-backdrop" onMouseDown={e=>{if(e.target===e.currentTarget)close()}}>
    <div className="network-modal mainnet" role="dialog" aria-modal="true" aria-labelledby="network-mainnet-title">
      <span className="network-modal-kicker">MAINNET</span>
      <h3 id="network-mainnet-title">Attenzione, stai per passare alla MAINNET</h3>
      <p>La MAINNET utilizza fondi reali. TRAXION rimuoverà automaticamente la credenziale {me.follower_network.toUpperCase()} attuale e, completato lo switch, mostrerà subito i campi per inserire <b>API Wallet Address</b> e <b>Private Key</b> della MAINNET.</p>
      <p className="muted">La strategia resterà in PAUSA dopo il cambio rete finché non configuri il nuovo API Wallet e scegli di riattivarla.</p>
      <div className="network-modal-actions"><button onClick={close}>Annulla</button><button className="primary" onClick={confirm}>Passa a MAINNET</button></div>
    </div>
  </div>;
  return <div className="network-modal-backdrop" onMouseDown={e=>{if(e.target===e.currentTarget)close()}}>
    <div className="network-modal" role="dialog" aria-modal="true" aria-labelledby="network-blocked-title">
      <span className="network-modal-kicker">Cambio rete → {targetLabel}</span>
      <h3 id="network-blocked-title">Completa prima questi passaggi</h3>
      <p>Il toggle si sbloccherà automaticamente appena TRAXION rileva tutte le condizioni completate.</p>
      <div className="network-requirements">{requirements.map(([code,label])=>{
        const pending=blocked.has(code);
        return <div key={code} className={`network-requirement ${pending?'pending':'done'}`}><span>{pending?'!':'✓'}</span><strong>{label}</strong></div>;
      })}</div>
      <div className="network-modal-actions"><button onClick={close}>Chiudi</button><button className="primary" onClick={refreshState}>Aggiorna stato</button></div>
    </div>
  </div>;
}

function BasicRisk({risk,equity,profile,markets,setProfile,setMarkets,apply,openPro}:{
  risk:Risk;
  equity:number|null;
  profile:BasicProfile;
  markets:BasicMarkets;
  setProfile:(v:BasicProfile)=>void;
  setMarkets:(v:BasicMarkets)=>void;
  apply:()=>void;
  openPro:()=>void;
}){
  const preview=buildBasicRisk(risk,profile,markets,equity);
  return <div className="basic-risk">
    <div className="basic-section">
      <div className="basic-section-title"><span>1</span><div><h3>Quanto vuoi seguire la strategia?</h3><p>TRAXION traduce questa scelta in sizing, leva e limiti tecnici del Risk Engine.</p></div></div>
      <div className="risk-choice-grid">
        {(Object.keys(BASIC_PROFILES) as Exclude<BasicProfile,'custom'>[]).map(key=>{
          const item=BASIC_PROFILES[key];
          return <button key={key} className={`risk-choice ${profile===key?'active':''}`} onClick={()=>setProfile(key)}>
            <div className="risk-choice-top"><strong>{item.title}</strong>{item.badge&&<span className="choice-badge">{item.badge}</span>}</div>
            <span>{item.description}</span>
            <small>Intensità {Math.round(item.multiplier*100)}% · leva max {item.maxLeverage}× · drawdown {item.maxDrawdown}%</small>
          </button>;
        })}
        {profile==='custom'&&<button className="risk-choice active custom" onClick={openPro}>
          <div className="risk-choice-top"><strong>Personalizzato</strong><span className="choice-badge">PRO</span></div>
          <span>I valori attuali non corrispondono a un preset Basic.</span>
          <small>Apri Pro per vedere o mantenere la configurazione esatta.</small>
        </button>}
      </div>
    </div>

    <div className="basic-section">
      <div className="basic-section-title"><span>2</span><div><h3>Quali mercati vuoi utilizzare?</h3><p>Gli asset non disponibili sulla rete del tuo account vengono ignorati automaticamente.</p></div></div>
      <div className="market-choice-grid">
        <button className={`market-choice ${markets==='all'?'active':''}`} onClick={()=>setMarkets('all')}><strong>Tutti</strong><span>Tutti i perpetual supportati</span></button>
        <button className={`market-choice ${markets==='majors'?'active':''}`} onClick={()=>setMarkets('majors')}><strong>Major</strong><span>BTC, ETH, SOL e principali large cap</span></button>
        <button className={`market-choice ${markets==='btc'?'active':''}`} onClick={()=>setMarkets('btc')}><strong>Solo BTC</strong><span>Un solo mercato, massima semplicità</span></button>
        {markets==='custom'&&<button className="market-choice active custom" onClick={openPro}><strong>Personalizzati</strong><span>Whitelist/blacklist definite in modalità Pro</span></button>}
      </div>
    </div>

    <div className="basic-summary">
      <div className="basic-summary-head"><div><h3>Riepilogo</h3><p>Questi sono i principali limiti che TRAXION applicherà alla strategia.</p></div>{equity!=null&&<span className="badge">Equity {usd(equity)}</span>}</div>
      <div className="basic-summary-grid">
        <SummaryItem label="Intensità strategia" value={`${Math.round(Number(preview.multiplier)*100)}%`}/>
        <SummaryItem label="Leva account" value={`Strategia fino a ${Number(preview.max_leverage)}×`}/>
        <SummaryItem label="Soglia drawdown" value={`${Number(preview.max_drawdown_pct)}%`}/>
        <SummaryItem label="Perdita giornaliera" value={`${Number(preview.max_daily_loss_pct)}%`}/>
        <SummaryItem label="Max posizioni" value={String(preview.max_positions)}/>
        <SummaryItem label="Mercati" value={marketLabel(markets)}/>
      </div>
      <p className="basic-footnote">I tetti monetari vengono calibrati automaticamente sull'equity disponibile quando è presente. La leva viene allineata alla strategia sorgente ma non può superare il limite del profilo o quello del singolo mercato.</p>
      <div className="actions basic-actions">
        <button className="primary" onClick={apply} disabled={profile==='custom'}>Applica profilo Basic</button>
        <button onClick={openPro}>Vedi parametri Pro</button>
      </div>
    </div>
  </div>;
}

function SummaryItem({label,value}:{label:string;value:string}){
  return <div className="summary-item"><span>{label}</span><strong>{value}</strong></div>;
}

function buildBasicRisk(risk:Risk,profile:BasicProfile,markets:BasicMarkets,equity:number|null):Risk{
  let next:Risk={...risk,allow_assets:[...risk.allow_assets],block_assets:[...risk.block_assets]};
  if(profile!=='custom'){
    const p=BASIC_PROFILES[profile];
    next={
      ...next,
      multiplier:cleanNumber(p.multiplier),
      max_leverage:cleanNumber(p.maxLeverage),
      max_positions:p.maxPositions,
      max_drawdown_pct:cleanNumber(p.maxDrawdown),
      max_daily_loss_pct:cleanNumber(p.dailyLoss),
      min_notional:'10',
      max_slippage_bps:50,
      close_only:false,
    };
    if(equity!=null&&equity>0){
      const total=Math.max(10,roundMoney(equity*p.exposureFactor));
      const asset=Math.max(10,Math.min(total,roundMoney(total*p.assetShare)));
      next.max_total_exposure=cleanNumber(total);
      next.max_asset_exposure=cleanNumber(asset);
      next.max_notional_per_trade=cleanNumber(asset);
    }
  }

  if(markets!=='custom'){
    next.allow_assets=markets==='all'?[]:markets==='btc'?['BTC']:[...MAJOR_ASSETS];
    next.block_assets=[];
  }
  return next;
}

function inferBasicProfile(risk:Risk):BasicProfile{
  const candidates=(Object.keys(BASIC_PROFILES) as Exclude<BasicProfile,'custom'>[]);
  for(const key of candidates){
    const p=BASIC_PROFILES[key];
    if(close(Number(risk.multiplier),p.multiplier)&&close(Number(risk.max_leverage),p.maxLeverage)&&close(Number(risk.max_drawdown_pct),p.maxDrawdown)&&close(Number(risk.max_daily_loss_pct),p.dailyLoss))return key;
  }
  return 'custom';
}

function inferBasicMarkets(risk:Risk):BasicMarkets{
  if(risk.block_assets.length)return 'custom';
  if(risk.allow_assets.length===0)return 'all';
  if(sameAssets(risk.allow_assets,['BTC']))return 'btc';
  if(sameAssets(risk.allow_assets,MAJOR_ASSETS))return 'majors';
  return 'custom';
}

function sameAssets(a:string[],b:string[]){
  const aa=[...a].map(x=>x.toUpperCase()).sort();
  const bb=[...b].map(x=>x.toUpperCase()).sort();
  return aa.length===bb.length&&aa.every((x,i)=>x===bb[i]);
}

function marketLabel(markets:BasicMarkets){
  if(markets==='all')return 'Tutti';
  if(markets==='majors')return 'Major';
  if(markets==='btc')return 'Solo BTC';
  return 'Personalizzati';
}

function cleanNumber(value:number){return Number(value.toFixed(2)).toString()}
function roundMoney(value:number){return Math.round(value*100)/100}
function close(a:number,b:number){return Number.isFinite(a)&&Math.abs(a-b)<0.0001}
function usd(value:number){return value.toLocaleString('en-US',{style:'currency',currency:'USD',maximumFractionDigits:2})}

function Num({label,value,set}:{label:string;value:string;set:(v:string)=>void}){
  return <label>{label}<input type="number" step="any" value={value} onChange={e=>set(e.target.value)}/></label>;
}
