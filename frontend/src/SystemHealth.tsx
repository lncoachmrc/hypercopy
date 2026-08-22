import {useEffect,useState} from 'react';
import {get} from './api';

type RateLimitSnapshot={
  status?:string;
  window_seconds?:number;
  total_budget?:number;
  reserve?:number;
  used?:number;
  used_pct?:number;
  lane_limits?:Record<string,number>;
  lane_reconcile?:number;
  lane_diagnostic?:number;
  lane_metadata?:number;
  lane_master_state?:number;
  lane_order?:number;
};

type AdminHealth={
  master_checkpoint:{
    configured:boolean;
    present:boolean;
    enabled:boolean;
    time_ms:number;
    updated_at:string|null;
    network:string;
  };
  ai_intelligence:{
    status:string;
    mode:string|null;
    provider:string|null;
    model:string|null;
    updated_at:string|null;
  };
};

type SystemHealthProps={
  sys:null|{
    rate_limit:Record<string,unknown>;
    flags:Record<string,boolean>;
    live_trading_env_enabled?:boolean;
  };
};

type GateTone='ok'|'warn'|'danger'|'neutral';

const LANES=[
  {key:'order',label:'Ordini',usage:'lane_order',description:'Invio e verifica delle azioni exchange.'},
  {key:'master_state',label:'Master state',usage:'lane_master_state',description:'Equity, posizioni e leva della strategia sorgente.'},
  {key:'reconcile',label:'Reconciliation',usage:'lane_reconcile',description:'Verifica e riallineamento degli account utenti.'},
  {key:'metadata',label:'Metadata',usage:'lane_metadata',description:'Specifiche asset e metadati Hyperliquid.'},
  {key:'diagnostic',label:'Diagnostica',usage:'lane_diagnostic',description:'Letture amministrative on-demand.'},
] as const;

export default function SystemHealth({sys}:SystemHealthProps){
  const [health,setHealth]=useState<AdminHealth|null>(null);
  const rate=(sys?.rate_limit||{}) as RateLimitSnapshot;
  const flags=sys?.flags||{};
  const rateAvailable=typeof rate.total_budget==='number'&&typeof rate.used==='number';
  const globalPct=rateAvailable?percentage(rate.used||0,rate.total_budget||0):null;
  const liveFlag=flags.live_trading===true;
  const envGate=sys?.live_trading_env_enabled===true;
  const liveTradingEffective=liveFlag&&envGate;
  const checkpoint=health?.master_checkpoint;
  const checkpointOk=Boolean(checkpoint?.configured&&checkpoint.present&&checkpoint.enabled&&checkpoint.time_ms>0);
  const ai=health?.ai_intelligence;
  const aiTone=aiStatusTone(ai?.status);
  const aiMode=ai?.mode?.toUpperCase()||'—';

  useEffect(()=>{
    let alive=true;
    const load=async()=>{try{const value=await get<AdminHealth>('/admin/health');if(alive)setHealth(value)}catch{/* /admin/system remains available even if this diagnostic read fails */}};
    void load();
    const timer=window.setInterval(()=>void load(),10000);
    return()=>{alive=false;window.clearInterval(timer)};
  },[]);

  return <section className="panel">
    <div className="panelhead">
      <div>
        <h2>Salute sistema</h2>
        <p className="muted">Gate operativi e consumo del budget Hyperliquid. Questi indicatori sono visibili solo agli amministratori.</p>
      </div>
      {rateAvailable?<HealthBadge tone={rateTone(globalPct)}>{`${rate.used} / ${rate.total_budget} · ${formatPct(globalPct)}`}</HealthBadge>:<HealthBadge tone="warn">Budget non disponibile</HealthBadge>}
    </div>

    <div className="stats">
      <HealthCard label="Live trading" value={liveTradingEffective?'ON':'OFF'} tone={liveTradingEffective?'ok':liveFlag||envGate?'warn':'neutral'} hint={`Effettivo MAINNET · flag DB ${liveFlag?'ON':'OFF'} · gate ambiente ${envGate?'ON':'OFF'}`}/>
      <HealthCard label="Global pause" value={flags.global_pause?'ATTIVA':'OK'} tone={flags.global_pause?'warn':'ok'} hint="Blocca l’esecuzione globale quando attiva."/>
      <HealthCard label="Emergency stop" value={flags.emergency_stop?'ATTIVO':'OK'} tone={flags.emergency_stop?'danger':'ok'} hint="Stato di arresto di emergenza."/>
      <HealthCard label="Master checkpoint" value={!checkpoint?'—':checkpointOk?'OK':'KO'} tone={!checkpoint?'neutral':checkpointOk?'ok':'danger'} hint={checkpointHint(checkpoint)}/>
      <HealthCard label="AI intelligence" value={ai?.status?ai.status.toUpperCase():'—'} tone={aiTone} hint={aiHint(ai)}/>
      <HealthCard label="Modalità AI" value={aiMode} tone="neutral" hint="Analisi consultiva: l’AI non invia ordini direttamente. Risk Engine e controlli deterministici restano autoritativi."/>
    </div>

    {rateAvailable?<>
      <div className="panelhead" style={{marginTop:18}}>
        <div><h3>Budget Hyperliquid</h3><p className="muted">Finestra {rate.window_seconds??60}s · riserva operativa {rate.reserve??'—'} weight.</p></div>
      </div>
      <table>
        <thead><tr><th>Lane</th><th>Uso</th><th>Limite</th><th>Utilizzo</th><th>Stato</th><th>Funzione</th></tr></thead>
        <tbody>{LANES.map(lane=>{
          const used=numberField(rate,lane.usage);
          const limit=rate.lane_limits?.[lane.key];
          const pct=typeof limit==='number'?percentage(used,limit):null;
          return <tr key={lane.key}>
            <td><b>{lane.label}</b></td>
            <td>{used}</td>
            <td>{limit??'—'}</td>
            <td className={pct!=null&&pct>=90?'down':pct!=null&&pct<75?'up':''}>{formatPct(pct)}</td>
            <td><HealthBadge tone={rateTone(pct)}>{rateLabel(pct)}</HealthBadge></td>
            <td className="muted">{lane.description}</td>
          </tr>;
        })}</tbody>
      </table>
    </>:<div className="alert error">Rate limiter non leggibile: {rate.status||'stato Redis non disponibile'}.</div>}

    <details style={{marginTop:18}}>
      <summary>Dettagli tecnici</summary>
      <pre>{JSON.stringify({flags,rate_limit:rate,health},null,2)}</pre>
    </details>
  </section>;
}

function HealthCard({label,value,tone,hint}:{label:string;value:string;tone:GateTone;hint:string}){
  return <div className="panel stat"><span>{label}</span><strong className={tone==='danger'?'down':tone==='ok'?'up':''}>{value}</strong><small className="muted">{hint}</small></div>;
}

function HealthBadge({tone,children}:{tone:GateTone;children:React.ReactNode}){
  const cls=tone==='ok'?'badge live':tone==='danger'?'badge offline':'badge';
  return <span className={cls}>{children}</span>;
}

function checkpointHint(checkpoint:AdminHealth['master_checkpoint']|undefined){
  if(!checkpoint)return 'Lettura checkpoint in corso.';
  if(!checkpoint.configured)return 'Sorgente master non configurata.';
  if(!checkpoint.present||!checkpoint.enabled||checkpoint.time_ms<=0)return `Nessun checkpoint valido per la sorgente ${checkpoint.network.toUpperCase()} corrente.`;
  return `Ultimo evento checkpoint: ${new Date(checkpoint.time_ms).toLocaleString()}.`;
}

function aiHint(ai:AdminHealth['ai_intelligence']|undefined){
  if(!ai)return 'Lettura stato runtime AI in corso.';
  const runtime=[ai.provider,ai.model].filter(Boolean).join(' · ');
  const updated=ai.updated_at?` · aggiornato ${new Date(ai.updated_at).toLocaleString()}`:'';
  return `${runtime||ai.mode||'runtime AI'}${updated}`;
}

function aiStatusTone(status:string|undefined):GateTone{
  if(status==='ok')return'ok';
  if(status==='degraded')return'warn';
  if(status==='disabled')return'neutral';
  if(status==='pending')return'neutral';
  return status?'warn':'neutral';
}

function numberField(rate:RateLimitSnapshot,key:keyof RateLimitSnapshot){
  const value=rate[key];
  return typeof value==='number'?value:0;
}

function percentage(used:number,limit:number){return limit>0?used/limit*100:null}
function formatPct(value:number|null){return value==null?'—':`${value.toFixed(1)}%`}
function rateTone(value:number|null):GateTone{return value==null?'neutral':value>=90?'danger':value>=75?'warn':'ok'}
function rateLabel(value:number|null){return value==null?'N/D':value>=90?'SATURO':value>=75?'ALTO':'OK'}
