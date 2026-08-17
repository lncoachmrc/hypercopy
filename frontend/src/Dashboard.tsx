import {useEffect,useState} from 'react';
import {Area,AreaChart,CartesianGrid,ReferenceLine,ResponsiveContainer,Tooltip,XAxis,YAxis} from 'recharts';
import {get,wsUrl} from './api';
import {useAuth} from './auth';

type Dash={equity:number|null;pnl_absolute:number;max_drawdown_pct:number;sharpe:number|null;positions:number;user:{copy_state:string;risk_state:string}};
type Pos={asset:string;current_size:string;target_size:string;delta:string;mark_price:string;delta_notional:string;status:'READY'|'BELOW_MIN'|'UNAVAILABLE'|'ON_TARGET';reason:string|null;managed:boolean;exchange_verified_at:string|null};
type Exec={id:string;asset:string;state:string;is_buy:boolean;requested_size:string;filled_size:string;avg_price:string|null;reduce_only:boolean;reject_reason:string|null;created_at:string};
type PnlRange='1d'|'7d'|'30d'|'90d'|'all';
type PnlPoint={at:string;value:number;bucket_value:number};
type PnlHistory={range:PnlRange;pnl_absolute:number;pnl_pct:number|null;start_equity:number|null;current_equity:number|null;points:PnlPoint[];source:'realized_net'};
type CardTone='positive'|'negative'|'neutral';

const RANGE_OPTIONS:{key:PnlRange;label:string}[]=[
  {key:'1d',label:'1D'},
  {key:'7d',label:'7D'},
  {key:'30d',label:'30D'},
  {key:'90d',label:'90D'},
  {key:'all',label:'All'},
];

export default function Dashboard(){
  const {user}=useAuth();
  const [d,setD]=useState<Dash|null>(null);
  const [pos,setPos]=useState<Pos[]>([]);
  const [execs,setExec]=useState<Exec[]>([]);
  const [live,setLive]=useState('connecting');
  const [range,setRange]=useState<PnlRange>('1d');
  const [pnl,setPnl]=useState<PnlHistory|null>(null);
  const [pnlError,setPnlError]=useState('');

  const load=async()=>{
    const [a,b,c]=await Promise.all([
      get<Dash>('/dashboard'),
      get<Pos[]>('/positions'),
      get<Exec[]>('/executions?limit=25'),
    ]);
    setD(a);setPos(b);setExec(c);
  };

  const loadPnl=async(selected:PnlRange)=>{
    try{
      setPnlError('');
      setPnl(await get<PnlHistory>(`/pnl-history?range=${selected}`));
    }catch(e){
      setPnlError(e instanceof Error?e.message:'Errore caricamento PnL');
    }
  };

  useEffect(()=>{
    void load();
    const ws=new WebSocket(wsUrl());
    ws.onopen=()=>setLive('live');
    ws.onclose=()=>setLive('offline');
    ws.onmessage=e=>{try{const m=JSON.parse(e.data);if(m.type!=='heartbeat')void load();}catch{}};
    return()=>ws.close();
  },[]);

  useEffect(()=>{
    void loadPnl(range);
    const timer=window.setInterval(()=>void loadPnl(range),30000);
    return()=>window.clearInterval(timer);
  },[range]);

  return <>
    <div className="title"><div><h1>Dashboard</h1><p>Stato reale, target e delta del tuo account Hyperliquid.</p></div><span className={`badge ${live}`}>{live}</span></div>
    <div className="stats"><Card label="Equity" value={money(d?.equity)}/><Card label="PnL periodo" value={money(d?.pnl_absolute)} tone={pnlTone(d?.pnl_absolute)}/><Card label="Max drawdown" value={d?`${d.max_drawdown_pct.toFixed(2)}%`:'—'}/><Card label="Sharpe" value={d?.sharpe==null?'—':d.sharpe.toFixed(2)}/></div>

    <PnlChart data={pnl} range={range} setRange={setRange} error={pnlError}/>

    <section className="panel"><div className="panelhead"><h2>Position targeting</h2><span className="badge">{d?.user.copy_state||user?.copy_state} · {d?.user.risk_state||'NORMAL'}</span></div><table><thead><tr><th>Asset</th><th>Attuale</th><th>Target</th><th>Delta</th><th>Stato</th><th>Verifica exchange</th></tr></thead><tbody>{pos.length?pos.map(p=><tr key={p.asset}><td><b>{p.asset}</b></td><td>{p.current_size}</td><td>{p.status==='UNAVAILABLE'?'—':p.target_size}</td><td className={p.status==='UNAVAILABLE'?'':Number(p.delta)>=0?'up':'down'}>{p.status==='UNAVAILABLE'?'—':p.delta}</td><td><TargetStatus p={p}/></td><td>{p.exchange_verified_at?new Date(p.exchange_verified_at).toLocaleString():'—'}</td></tr>):<tr><td colSpan={6}>Nessuna posizione gestita.</td></tr>}</tbody></table></section>
    <section className="panel"><div className="panelhead"><h2>Ultime execution</h2><span className="muted">Cloid persistente + reconciliation</span></div><table><thead><tr><th>Ora</th><th>Asset</th><th>Lato</th><th>Size</th><th>Stato</th><th>Motivo</th></tr></thead><tbody>{execs.map(x=><tr key={x.id}><td>{new Date(x.created_at).toLocaleString()}</td><td>{x.asset}</td><td className={x.is_buy?'up':'down'}>{x.is_buy?'BUY':'SELL'}{x.reduce_only?' RO':''}</td><td>{x.requested_size}</td><td><span className="badge">{x.state}</span></td><td>{x.reject_reason||'—'}</td></tr>)}</tbody></table></section>
  </>;
}

function PnlChart({data,range,setRange,error}:{data:PnlHistory|null;range:PnlRange;setRange:(r:PnlRange)=>void;error:string}){
  const positive=(data?.pnl_absolute??0)>=0;
  const stroke=positive?'#66d9a2':'#ff7b89';
  const fillId=positive?'pnlPositiveFill':'pnlNegativeFill';
  return <section className="panel pnl-panel">
    <div className="panelhead pnl-chart-head">
      <h2>Andamento PnL</h2>
      <div className="pnl-ranges" aria-label="Intervallo grafico PnL">
        {RANGE_OPTIONS.map(x=><button key={x.key} className={range===x.key?'active':''} onClick={()=>setRange(x.key)}>{x.label}</button>)}
      </div>
    </div>

    {error?<div className="pnl-empty">{error}</div>:<div className="pnl-chart">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data?.points||[]} margin={{top:12,right:12,bottom:0,left:0}}>
          <defs>
            <linearGradient id="pnlPositiveFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#66d9a2" stopOpacity={0.28}/><stop offset="100%" stopColor="#66d9a2" stopOpacity={0.01}/></linearGradient>
            <linearGradient id="pnlNegativeFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#ff7b89" stopOpacity={0.25}/><stop offset="100%" stopColor="#ff7b89" stopOpacity={0.01}/></linearGradient>
          </defs>
          <CartesianGrid stroke="#222a38" vertical={false}/>
          <XAxis dataKey="at" tickFormatter={v=>timeTick(String(v),range)} tick={{fill:'#7f8ba0',fontSize:11}} axisLine={{stroke:'#303849'}} tickLine={false} minTickGap={26}/>
          <YAxis tickFormatter={v=>pnlAxis(Number(v))} tick={{fill:'#7f8ba0',fontSize:11}} axisLine={false} tickLine={false} width={70} domain={['auto','auto']}/>
          <ReferenceLine y={0} stroke="#465064" strokeDasharray="4 4"/>
          <Tooltip content={<PnlTooltip/>}/>
          <Area type="monotone" dataKey="value" stroke={stroke} strokeWidth={2.2} fill={`url(#${fillId})`} isAnimationActive={false} dot={false} activeDot={{r:4}}/>
        </AreaChart>
      </ResponsiveContainer>
    </div>}
    <div className="pnl-foot">Il grafico usa esclusivamente PnL chiuso meno fee. Depositi, prelievi e variazioni di collateral non vengono conteggiati come profitto.</div>
  </section>;
}

function PnlTooltip({active,payload,label}:any){
  if(!active||!payload?.length)return null;
  return <div className="pnl-tooltip"><strong>{signedMoney(Number(payload[0].value))}</strong><span>{new Date(String(label)).toLocaleString()}</span></div>;
}

function timeTick(value:string,range:PnlRange){
  const d=new Date(value);
  if(range==='1d')return d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
  if(range==='7d')return d.toLocaleDateString([],{weekday:'short',hour:'2-digit'});
  return d.toLocaleDateString([],{day:'2-digit',month:'short'});
}

function pnlAxis(v:number){
  const sign=v>0?'+':'';
  return `${sign}$${Math.abs(v)<10?v.toFixed(2):v.toFixed(0)}`;
}

function TargetStatus({p}:{p:Pos}){if(p.status==='UNAVAILABLE')return <span className="badge" title={p.reason||''}>Non disponibile TESTNET</span>;if(p.status==='BELOW_MIN')return <span className="badge" title={p.reason||''}>Sotto minimo · ${Number(p.delta_notional).toFixed(2)}</span>;if(p.status==='READY')return <span className="badge live">Pronto · ${Number(p.delta_notional).toFixed(2)}</span>;return <span className="muted">Allineato</span>}
function Card({label,value,tone='neutral'}:{label:string;value:string;tone?:CardTone}){const cls=tone==='positive'?'up':tone==='negative'?'down':'';return <div className="panel stat"><span>{label}</span><strong className={cls}>{value}</strong></div>}
function pnlTone(v:number|null|undefined):CardTone{return v==null||v===0?'neutral':v>0?'positive':'negative'}
function money(v:number|null|undefined){return v==null?'—':v.toLocaleString('en-US',{style:'currency',currency:'USD',maximumFractionDigits:2})}
function signedMoney(v:number|null|undefined){if(v==null)return'—';const abs=Math.abs(v).toLocaleString('en-US',{style:'currency',currency:'USD',maximumFractionDigits:2});return `${v>0?'+':v<0?'-':''}${abs}`}
