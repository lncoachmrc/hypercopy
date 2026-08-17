import {useEffect,useState} from 'react';
import {get} from './api';

type AiState={status:string;mode:string;provider?:string;model?:string;preferred_model?:string;fallback_index?:number;updated_at?:string|null;analysis?:{summary?:string;confidence?:number;capital_policy?:{buffer_pct?:number;preferred_coverage_pct?:number;minimum_coverage_pct?:number;micro_position_policy?:string;rebalance_urgency?:string}};capital_efficiency?:{coverage_pct:number|null;executable_positions:number;managed_positions:number;below_min_positions:number}};

export default function AiStatus(){
  const [ai,setAi]=useState<AiState|null>(null);
  useEffect(()=>{
    let alive=true;
    const load=async()=>{try{const value=await get<AiState>('/ai/intelligence');if(alive)setAi(value)}catch{}};
    void load();
    const timer=window.setInterval(()=>void load(),60000);
    return()=>{alive=false;window.clearInterval(timer)};
  },[]);
  if(!ai)return <span className="badge">AI · loading</span>;
  const active=ai.model?`${ai.provider||'AI'} · ${ai.model}`:ai.preferred_model||'AI non configurata';
  const fallback=(ai.fallback_index||0)>0?` · fallback ${ai.fallback_index}`:'';
  const title=[
    ai.analysis?.summary,
    ai.capital_efficiency?.coverage_pct!=null?`Capital coverage: ${ai.capital_efficiency.coverage_pct}%`:null,
    ai.updated_at?`Updated: ${new Date(ai.updated_at).toLocaleString()}`:null,
  ].filter(Boolean).join('\n');
  return <span className={`badge ${ai.status==='ok'?'live':''}`} title={title}>AI {ai.mode.toUpperCase()} · {active}{fallback}</span>;
}
