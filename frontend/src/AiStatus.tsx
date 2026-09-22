import {useCallback,useEffect,useState} from 'react';
import {get,post} from './api';
import {useAuth} from './auth';
import './ai-status.css';

type AiState={status:string;mode:string;requested_mode?:string;effective_mode?:string;execution_influence?:boolean;execution_influence_requested?:boolean;execution_factor?:string;execution_buffer_pct?:string;fallback_reason?:string|null;safety?:string;provider?:string;model?:string;preferred_model?:string;fallback_index?:number;updated_at?:string|null;analysis?:{summary?:string;confidence?:number;capital_policy?:{buffer_pct?:number;preferred_coverage_pct?:number;minimum_coverage_pct?:number;micro_position_policy?:string;rebalance_urgency?:string}};capital_efficiency?:{coverage_pct:number|null;executable_positions:number;managed_positions:number;below_min_positions:number}};
type AiModeResponse={ok:boolean;mode:string;effective_mode:string;requested_mode:string;execution_influence:boolean;execution_factor:string;takes_effect:string};
type ProfitExitMode='OFF'|'SHADOW'|'ON';
type ProfitExitModeState={mode:ProfitExitMode;source:string;evaluates:boolean;operational:boolean;reason?:string|null;updated_at?:string|null};

const AI_MODE_CHANGED='traxion:ai-mode-changed';

function useAiState(){
  const [ai,setAi]=useState<AiState|null>(null);
  const load=useCallback(async()=>{try{setAi(await get<AiState>('/ai/intelligence'))}catch{}},[]);
  useEffect(()=>{
    void load();
    const timer=window.setInterval(()=>void load(),60000);
    const refresh=()=>void load();
    window.addEventListener(AI_MODE_CHANGED,refresh);
    return()=>{window.clearInterval(timer);window.removeEventListener(AI_MODE_CHANGED,refresh)};
  },[load]);
  return {ai,load};
}

export default function AiStatus(){
  const {ai}=useAiState();
  if(!ai)return <span className="badge">AI · loading</span>;
  const active=ai.model?`${ai.provider||'AI'} · ${ai.model}`:ai.preferred_model||'AI non configurata';
  const fallback=(ai.fallback_index||0)>0?` · fallback ${ai.fallback_index}`:'';
  const title=[
    ai.analysis?.summary,
    ai.execution_factor?`Execution factor: ${ai.execution_factor}`:null,
    ai.fallback_reason,
    ai.safety,
    ai.capital_efficiency?.coverage_pct!=null?`Capital coverage: ${ai.capital_efficiency.coverage_pct}%`:null,
    ai.updated_at?`Updated: ${new Date(ai.updated_at).toLocaleString()}`:null,
  ].filter(Boolean).join('\n');
  return <span className={`badge ${ai.status==='ok'?'live':''}`} title={title}>AI {ai.mode.toUpperCase()} · {active}{fallback}</span>;
}

export function AiModeToggle(){
  const {user}=useAuth();
  const {ai,load}=useAiState();
  const [saving,setSaving]=useState(false);
  const [error,setError]=useState('');
  if(user?.role!=='SUPERADMIN')return null;

  const requested=(ai?.requested_mode||ai?.mode||'shadow').toLowerCase();
  const effective=(ai?.effective_mode||ai?.mode||'shadow').toLowerCase();
  const change=async(next:'shadow'|'on')=>{
    if(saving||requested===next)return;
    if(next==='on'){
      const confirmed=window.confirm(
        'Attivare AI ON? La Capital Intelligence potrà solo ridurre in modo conservativo il target di capitale. Non può creare ordini né superare il Risk Engine.'
      );
      if(!confirmed)return;
    }
    setSaving(true);setError('');
    try{
      await post<AiModeResponse>('/ai/mode',{mode:next,reason:'Dashboard AI mode toggle'});
      await load();
      window.dispatchEvent(new Event(AI_MODE_CHANGED));
    }catch(e){
      setError(e instanceof Error?e.message:'Cambio modalità AI non riuscito');
    }finally{setSaving(false)}
  };

  return <div className="ai-mode-control">
    <div className="ai-mode-control-head">
      <span>Modalità AI</span>
      <strong className={effective==='on'?'up':''}>{effective==='on'?'AI ON':'AI SHADOW'}</strong>
    </div>
    <div className="ai-mode-toggle" role="group" aria-label="Modalità operativa AI">
      <button type="button" className={requested==='shadow'?'active':''} disabled={saving||!ai} onClick={()=>void change('shadow')}>SHADOW</button>
      <button type="button" className={requested==='on'?'active on':''} disabled={saving||!ai||ai.status!=='ok'} title={ai?.status!=='ok'?'AI ON richiede intelligence in stato OK':''} onClick={()=>void change('on')}>ON</button>
    </div>
    {requested==='on'&&effective!=='on'&&<small className="ai-mode-fallback">Fail-safe SHADOW · ultimo fattore sicuro {ai?.execution_factor||'—'}</small>}
    {error&&<small className="ai-mode-error">{error}</small>}
  </div>;
}


export function AiProfitExitModeControl(){
  const {user}=useAuth();
  const [state,setState]=useState<ProfitExitModeState|null>(null);
  const [saving,setSaving]=useState(false);
  const [error,setError]=useState('');
  const load=useCallback(async()=>{
    if(user?.role!=='SUPERADMIN')return;
    try{setState(await get<ProfitExitModeState>('/ai/profit-exit-mode'))}
    catch(e){setError(e instanceof Error?e.message:'Lettura AI Profit Exit non riuscita')}
  },[user?.role]);

  useEffect(()=>{void load()},[load]);
  if(user?.role!=='SUPERADMIN')return null;

  const change=async(next:ProfitExitMode)=>{
    if(saving||state?.mode===next)return;
    if(next==='ON'){
      const confirmed=window.confirm(
        'Attivare AI Profit Exit ON? L’AI potrà richiedere la chiusura completa di posizioni follower già in profitto. Ogni ordine resta reduce-only e deve superare Risk Engine e revalidation economica finale.'
      );
      if(!confirmed)return;
    }
    setSaving(true);setError('');
    try{
      const updated=await post<ProfitExitModeState & {ok:boolean}>(
        '/ai/profit-exit-mode',
        {mode:next,reason:'Dashboard AI Profit Exit mode toggle'}
      );
      setState(updated);
    }catch(e){
      setError(e instanceof Error?e.message:'Cambio AI Profit Exit non riuscito');
    }finally{
      setSaving(false);
    }
  };

  const mode=state?.mode||'OFF';
  return <section className="panel">
    <div className="panelhead">
      <div>
        <h2>AI Profit Exit</h2>
        <p className="muted">Controllo globale SUPERADMIN delle chiusure profittevoli decise dall’AI.</p>
      </div>
      <span className={`badge ${mode==='ON'?'live':''}`}>{mode}</span>
    </div>
    <div className="actions">
      <button type="button" disabled={saving||!state||mode==='OFF'} onClick={()=>void change('OFF')}>OFF</button>
      <button type="button" disabled={saving||!state||mode==='SHADOW'} onClick={()=>void change('SHADOW')}>SHADOW</button>
      <button type="button" className="primary" disabled={saving||!state||mode==='ON'} onClick={()=>void change('ON')}>ON</button>
      <button type="button" disabled={saving} onClick={()=>void load()}>Aggiorna</button>
    </div>
    <dl>
      <dt>OFF</dt><dd>Nessuna nuova valutazione AI Profit Exit operativa.</dd>
      <dt>SHADOW</dt><dd>L’AI valuta e registra HOLD / CLOSE_PROFIT / ABSTAIN senza creare ordini.</dd>
      <dt>ON</dt><dd>CLOSE_PROFIT può accodare una chiusura full residual reduce-only se il profitto netto residuo resta &gt; 0 e tutti i controlli deterministici passano.</dd>
      <dt>Fonte runtime</dt><dd>{state?.source||'—'}</dd>
      <dt>Ultimo aggiornamento</dt><dd>{state?.updated_at?new Date(state.updated_at).toLocaleString():'—'}</dd>
    </dl>
    <p className="muted">Il cambio è condiviso via PostgreSQL tra AI worker ed execution worker. OFF blocca nuove submission non ancora inviate; eventuali execution già inviate/ambigue continuano solo la riconciliazione idempotente.</p>
    {error&&<div className="toast">{error}</div>}
  </section>;
}
