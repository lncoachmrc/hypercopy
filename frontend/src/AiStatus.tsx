import {useCallback,useEffect,useState} from 'react';
import {get,post} from './api';
import {useAuth} from './auth';

type AiState={status:string;mode:string;requested_mode?:string;effective_mode?:string;execution_influence?:boolean;execution_influence_requested?:boolean;execution_factor?:string;execution_buffer_pct?:string;fallback_reason?:string|null;safety?:string;provider?:string;model?:string;preferred_model?:string;fallback_index?:number;updated_at?:string|null;analysis?:{summary?:string;confidence?:number;capital_policy?:{buffer_pct?:number;preferred_coverage_pct?:number;minimum_coverage_pct?:number;micro_position_policy?:string;rebalance_urgency?:string}};capital_efficiency?:{coverage_pct:number|null;executable_positions:number;managed_positions:number;below_min_positions:number}};
type AiModeResponse={ok:boolean;mode:string;effective_mode:string;requested_mode:string;execution_influence:boolean;execution_factor:string;takes_effect:string};

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
