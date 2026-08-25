import {useEffect,useMemo,useState} from 'react';
import {get,post} from './api';
import {translateText} from './autoTranslate';

type PlanSlug='starter'|'plus'|'pro_10k';
type DiscountUser={id:string;wallet:string;role:string;discounts:Partial<Record<PlanSlug,number>>};
type Overview={users:DiscountUser[];plans:PlanSlug[]};
type DiscountResult={ok:boolean;discounts:Partial<Record<PlanSlug,number>>;applies_to_existing_subscription:boolean};

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
  const [message,setMessage]=useState('');
  const [error,setError]=useState('');

  const load=async()=>{
    const data=await get<Overview>('/admin/plan-discounts?limit=100');
    setUsers(data.users);
    setEdits(current=>{
      const next={...current};
      for(const user of data.users){
        for(const plan of PLANS){
          const key=editKey(user.id,plan.slug);
          if(!(key in next))next[key]=String(user.discounts[plan.slug]??0);
        }
      }
      return next;
    });
  };

  useEffect(()=>{void load().catch(e=>setError(e instanceof Error?e.message:'Errore caricamento sconti'))},[]);

  const usersWithDiscounts=useMemo(
    ()=>users.filter(user=>PLANS.some(plan=>(user.discounts[plan.slug]??0)>0)).length,
    [users],
  );

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
      const result=await post<DiscountResult>(`/admin/users/${user.id}/plan-discounts/${plan}`,{
        percent_off:value,
        reason:'Commercial discount configured from Control Room',
        confirmation,
      });
      setUsers(current=>current.map(row=>row.id===user.id?{...row,discounts:result.discounts}:row));
      setEdits(current=>({...current,[key]:String(value)}));
      const label=PLANS.find(p=>p.slug===plan)?.label??plan;
      setMessage(value===0?`Sconto ${label} rimosso per ${user.wallet}.`:`Sconto ${label} ${value}% applicato a ${user.wallet}.`);
    }catch(e){
      setError(e instanceof Error?e.message:'Errore applicazione sconto');
    }finally{
      setBusy('');
    }
  };

  return <section className="panel" style={{marginTop:18}}>
    <div className="panelhead">
      <div>
        <h2>Sconti personali per wallet</h2>
        <p className="muted">Condizioni commerciali applicate automaticamente ai nuovi checkout Stripe. Ogni piano può avere una percentuale diversa da 0 a 100%.</p>
      </div>
      <div className="badge">{usersWithDiscounts} utenti con sconto</div>
    </div>
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
              <button disabled={busy!==''} onClick={()=>void apply(user,plan.slug)}>{busy===key?'…':'Applica'}</button>
              {current>0&&<button disabled={busy!==''} onClick={()=>{setEdits(values=>({...values,[key]:'0'}));void apply(user,plan.slug,0)}}>Rimuovi</button>}
            </div>
            <div className="muted" style={{marginTop:6,fontSize:12}}>Attuale: {current}%</div>
          </td>})}
        </tr>)}</tbody>
      </table>
    </div>
    <p className="muted" style={{marginTop:14}}>Lo sconto è associato all’account TRAXION e non richiede codici promozionali. Una sottoscrizione Stripe già attiva non viene modificata retroattivamente.</p>
  </section>;
}
