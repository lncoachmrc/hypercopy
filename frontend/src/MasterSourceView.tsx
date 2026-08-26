import {useEffect,useState} from 'react';
import {ApiError,get} from './api';
import Dashboard from './Dashboard';
import Settings from './Settings';


type MasterPosition={
  asset:string;
  size:string;
  entry_price:string;
  position_value:string;
  unrealized_pnl:string;
  leverage:number;
  margin_mode:'cross'|'isolated';
};

type MasterStatus={
  mode:'MASTER_SOURCE_READ_ONLY';
  network:'mainnet';
  address:string;
  account_value:number;
  collateral_balance:number;
  unrealized_pnl:number;
  free_margin:number;
  positions:MasterPosition[];
  follower_controls_enabled:false;
};

type PublicPerformance={
  current_pct:number;
  started_at:string;
  updated_at:string|null;
  network?:string;
  source?:string;
};

type SourceProbe={kind:'loading'}|{kind:'follower'}|{kind:'master';status:MasterStatus}|{kind:'error';message:string};

function useMasterSourceProbe(){
  const [probe,setProbe]=useState<SourceProbe>({kind:'loading'});
  useEffect(()=>{
    let cancelled=false;
    const load=async()=>{
      try{
        const status=await get<MasterStatus>('/master-source/status');
        if(!cancelled)setProbe({kind:'master',status});
      }catch(error){
        if(cancelled)return;
        if(error instanceof ApiError&&error.status===403){setProbe({kind:'follower'});return;}
        setProbe({kind:'error',message:error instanceof Error?error.message:'Master source status unavailable'});
      }
    };
    void load();
    return()=>{cancelled=true};
  },[]);
  return probe;
}

function money(value:number){return new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:2}).format(value)}
function signedMoney(value:number){return `${value>0?'+':''}${money(value)}`}
function shortAddress(value:string){return value.length>18?`${value.slice(0,10)}…${value.slice(-6)}`:value}

function SourceHeader(){return <div className="title"><div><h1>Master Source</h1><p>Wallet strategia Hyperliquid MAINNET osservato in sola lettura. TRAXION non invia ordini, non modifica la leva e non gestisce i fondi di questo account.</p></div><span className="badge live">MAINNET · READ ONLY</span></div>}

function SourceError({message}:{message:string}){return <><SourceHeader/><div className="alert error">{message}</div></>}

function MasterDashboard({initial}:{initial:MasterStatus}){
  const [status,setStatus]=useState(initial);
  const [performance,setPerformance]=useState<PublicPerformance|null>(null);
  useEffect(()=>{
    let cancelled=false;
    const load=async()=>{
      try{
        const [next,perf]=await Promise.all([
          get<MasterStatus>('/master-source/status'),
          get<PublicPerformance>('/public/master-performance?range=all'),
        ]);
        if(!cancelled){setStatus(next);setPerformance(perf)}
      }catch{/* keep last verified read-only snapshot */}
    };
    void load();
    const timer=window.setInterval(()=>void load(),30000);
    return()=>{cancelled=true;window.clearInterval(timer)};
  },[]);
  return <>
    <SourceHeader/>
    <div className="stats account-stats">
      <SourceCard label="Equity master" value={money(status.account_value)}/>
      <SourceCard label="Collateral" value={money(status.collateral_balance)}/>
      <SourceCard label="PnL aperto" value={signedMoney(status.unrealized_pnl)}/>
      <SourceCard label="Margine disponibile" value={money(status.free_margin)}/>
      <SourceCard label="Performance pubblica" value={performance?`${performance.current_pct>0?'+':''}${performance.current_pct.toFixed(2)}%`:'—'}/>
    </div>
    <section className="panel">
      <div className="panelhead"><h2>Sorgente strategia</h2><span className="badge live">Hyperliquid MAINNET</span></div>
      <dl>
        <dt>Wallet master</dt><dd title={status.address}>{shortAddress(status.address)}</dd>
        <dt>Modalità</dt><dd>MASTER SOURCE · READ ONLY</dd>
        <dt>Controlli follower</dt><dd>Disabilitati</dd>
        <dt>Baseline landing</dt><dd>26/08/2026 08:00 CEST</dd>
      </dl>
    </section>
    <section className="panel">
      <div className="panelhead"><h2>Posizioni reali del master</h2><span className="muted">Lettura diretta Hyperliquid</span></div>
      <table><thead><tr><th>Asset</th><th>Size</th><th>Entry</th><th>Valore posizione</th><th>PnL aperto</th><th>Leva</th></tr></thead><tbody>
        {status.positions.length?status.positions.map(p=><tr key={p.asset}><td><b>{p.asset}</b></td><td>{p.size}</td><td>{p.entry_price||'—'}</td><td>{money(Number(p.position_value))}</td><td>{signedMoney(Number(p.unrealized_pnl))}</td><td>{p.leverage}× {p.margin_mode}</td></tr>):<tr><td colSpan={6}>Nessuna posizione aperta sul wallet master MAINNET.</td></tr>}
      </tbody></table>
    </section>
  </>;
}

function MasterSettings({status}:{status:MasterStatus}){return <>
  <SourceHeader/>
  <section className="panel">
    <h2>Configurazione sorgente</h2>
    <p>Questo account rappresenta il portafoglio madre della strategia. La rete è MAINNET e il collegamento è esclusivamente in lettura.</p>
    <dl>
      <dt>Wallet master</dt><dd title={status.address}>{shortAddress(status.address)}</dd>
      <dt>Rete sorgente</dt><dd>MAINNET</dd>
      <dt>Operatività TRAXION sul master</dt><dd>Disabilitata</dd>
      <dt>Ordini / pausa / shadow / riconciliazione / chiusura</dt><dd>Non applicabili al master</dd>
      <dt>Destinazione dati</dt><dd>Copy engine follower + landing performance</dd>
    </dl>
    <div className="alert">Le strategie, il profilo rischio, la selezione TESTNET/MAINNET e le credenziali API Wallet restano disponibili esclusivamente agli account follower.</div>
  </section>
</>}

function SourceCard({label,value}:{label:string;value:string}){return <div className="stat"><span>{label}</span><strong>{value}</strong></div>}

export function MasterAwareDashboard(){
  const probe=useMasterSourceProbe();
  if(probe.kind==='loading')return <div className="center">Verifica sorgente master…</div>;
  if(probe.kind==='follower')return <Dashboard/>;
  if(probe.kind==='error')return <SourceError message={probe.message}/>;
  return <MasterDashboard initial={probe.status}/>;
}

export function MasterAwareSettings(){
  const probe=useMasterSourceProbe();
  if(probe.kind==='loading')return <div className="center">Verifica sorgente master…</div>;
  if(probe.kind==='follower')return <Settings/>;
  if(probe.kind==='error')return <SourceError message={probe.message}/>;
  return <MasterSettings status={probe.status}/>;
}
