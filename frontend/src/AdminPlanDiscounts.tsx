import {useEffect,useState} from 'react';
import {get,post} from './api';
import {translateText} from './autoTranslate';

type PlanSlug='starter'|'plus'|'pro_10k';
type DiscountUser={id:string;wallet:string;role:string;discounts:Partial<Record<PlanSlug,number>>};
type Overview={users:DiscountUser[];plans:PlanSlug[];total:number;discounted_total:number;offset:number;limit:number;wallet_query:string};
type DiscountResult={ok:boolean;discounts:Partial<Record<PlanSlug,number>>;applies_to_existing_subscription:boolean};

const PAGE_SIZE=50;
const PLANS:{slug:PlanSlug;label:string}[]=[
  {slug:'starter',label:'Starter'},
  {slug:'plus',label:'Plus'},
  {slug:'pro_10k',label:'Pro'},
];
const editKey=(userId:string,plan:PlanSlug)=>`${userId}:${plan}`;

export default function AdminPlanDiscounts(){
  const [users,setUsers]=useState<DiscountUser[]>([]);
  const [edits,setEdits]=useState<Record<string,string>>({});
  const [busy,setBusy]=useState('');
  const [loading,setLoading]=useState(false);
  const [message,setMessage]=useState('');
  const [error,setError]=useState('');
  const [total,setTotal]=useState(0);
  const [discountedTotal,setDiscountedTotal]=useState(0);
  const [offset,setOffset]=useState(0);
  const [walletInput,setWalletInput]=useState('');
  const [walletQuery,setWalletQuery]=useState('');

  const load=async(nextOffset:number,nextQuery:string)=>{
    setLoading(true);
    setError('');
    try{
      const params=new URLSearchParams({limit:String(PAGE_SIZE),offset:String(Math.max(nextOffset,0))});
      if(nextQuery)params.set('wallet',nextQuery);
      const data=await get<Overview>(`/admin/plan-discounts?${params.toString()}`);
      setUsers(data.users);
      setTotal(data.total);
      setDiscountedTotal(data.discounted_total);
      setOffset(data.offset);
      setWalletQuery(data.wallet_query);
      const nextEdits:Record<string,string>={};
      for(const user of data.users){
        for(const plan of PLANS)nextEdits[editKey(user.id,plan.slug)]=String(user.discounts[plan.slug]??0);
      }
      setEdits(nextEdits);
    }catch(e){
      setError(e instanceof Error?e.message:'Errore caricamento sconti');
    }finally{
      setLoading(false);
    }
  };

  useEffect(()=>{void load(0,'')},[]);

  const apply=async(user:DiscountUser,plan:PlanSlug,forcedValue?:number)=>{
    const key=editKey(user.id,plan);
    const raw=forcedValue==null?edits[key]:String(forcedValue);
    const value=Number(raw);
    setError('');setMessage('');
    if(!Number.isInteger(value)||value<0||value>100){setError('Inserisci una percentuale intera da 0 a 100.');return}
    let confirmation:string|undefined;
    if(value===100){
      const label=PLANS.find(p=>p.slug===plan)?.label??plan;
      if(!confirm(translateText(`Confermi lo sconto del 100% sul piano ${label} per ${user.wallet}? Il nuovo checkout del piano sarà gratuito.`)))return;
      confirmation='APPLY 100% DISCOUNT';
    }
    setBusy(key);
    try{
      await post<DiscountResult>(`/admin/users/${user.id}/plan-discounts/${plan}`,{
        percent_off:value,
        reason:'Commercial discount configured from Control Room',
        confirmation,
      });
      const label=PLANS.find(p=>p.slug===plan)?.label??plan;
      setMessage(value===0?`Sconto ${label} rimosso per ${user.wallet}.`:`Sconto ${label} ${value}% applicato a ${user.wallet}.`);
      await load(offset,walletQuery);
    }catch(e){
      setError(e instanceof Error?e.message:'Errore applicazione sconto');
    }finally{
      setBusy('');
    }
  };

  const start=total===0?0:offset+1;
  const end=Math.min(offset+users.length,total);
  const canPrev=offset>0&&!loading;
  const canNext=offset+users.length<total&&!loading;

  return <section className="panel" style={{marginTop:18}}>
    <div className="panelhead">
      <div>
        <h2>Sconti personali per wallet</h2>
        <p className="muted">Condizioni commerciali applicate automaticamente ai nuovi checkout Stripe. Ogni piano può avere una percentuale diversa da 0 a 100%.</p>
      </div>
      <div className="badge">{discountedTotal} utenti con sconto · {total} risultati</div>
    </div>
    <form onSubmit={e=>{e.preventDefault();const query=walletInput.trim();void load(0,query)}} style={{display:'flex',gap:8,alignItems:'center',flexWrap:'wrap',margin:'12px 0 16px'}}>
      <input aria-label="Cerca wallet" placeholder="Cerca wallet" value={walletInput} onChange={e=>setWalletInput(e.target.value)} style={{minWidth:300,flex:'1 1 320px'}}/>
      <button type="submit" disabled={loading}>Cerca</button>
      {(walletQuery||walletInput)&&<button type="button" disabled={loading} onClick={()=>{setWalletInput('');void load(0,'')}}>Azzera</button>}
    </form>
    {error&&<div className="alert error">{error}</div>}
    {message&&<div className="toast">{message}</div>}
    <div style={{overflowX:'auto'}}>
      <table>
        <thead><tr><th>Wallet</th>{PLANS.map(plan=><th key={plan.slug}>{plan.label}</th>)}</tr></thead>
        <tbody>{users.map(user=><tr key={user.id}>
          <td style={{minWidth:290}}><b style={{fontFamily:'monospace',wordBreak:'break-all'}}>{user.wallet}</b><div className="muted" style={{marginTop:4,fontSize:12}}>{user.role}</div></td>
          {PLANS.map(plan=>{const key=editKey(user.id,plan.slug);const current=user.discounts[plan.slug]??0;return <td key={plan.slug} style={{minWidth:230}}>
            <div style={{display:'flex',alignItems:'center',gap:7,flexWrap:'wrap'}}>
              <div style={{display:'flex',alignItems:'center',gap:4}}>
                <input aria-label={`Sconto ${plan.label} per ${user.wallet}`} type="number" min="0" max="100" step="1" inputMode="numeric" value={edits[key]??String(current)} onChange={e=>setEdits(values=>({...values,[key]:e.target.value}))} style={{width:76}}/>
                <span>%</span>
              </div>
              <button disabled={busy!==''||loading} onClick={()=>void apply(user,plan.slug)}>{busy===key?'…':'Applica'}</button>
              {current>0&&<button disabled={busy!==''||loading} onClick={()=>{setEdits(values=>({...values,[key]:'0'}));void apply(user,plan.slug,0)}}>Rimuovi</button>}
            </div>
            <div className="muted" style={{marginTop:6,fontSize:12}}>Attuale: {current}%</div>
          </td>})}
        </tr>)}{!loading&&users.length===0&&<tr><td colSpan={4} className="muted">Nessun wallet trovato.</td></tr>}</tbody>
      </table>
    </div>
    <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',gap:12,flexWrap:'wrap',marginTop:14}}>
      <span className="muted">Risultati {start}–{end} di {total}</span>
      <div style={{display:'flex',gap:8}}>
        <button disabled={!canPrev} onClick={()=>void load(Math.max(0,offset-PAGE_SIZE),walletQuery)}>Precedente</button>
        <button disabled={!canNext} onClick={()=>void load(offset+PAGE_SIZE,walletQuery)}>Successiva</button>
      </div>
    </div>
    <p className="muted" style={{marginTop:14}}>Lo sconto è associato all’account TRAXION e non richiede codici promozionali. Una sottoscrizione Stripe già attiva non viene modificata retroattivamente.</p>
  </section>;
}
