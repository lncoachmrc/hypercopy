import './billing.css';
import {useEffect,useMemo,useState} from 'react';
import {get,post} from './api';
import {detectLanguage,htmlLocaleByLanguage} from './i18n';

type Sub={entitled:boolean;status:string;plan:string|null;commercial_plan?:string|null;period_end:string|null;limits:Record<string,unknown>;portfolio_equity?:number|null;portfolio_limit_usd?:number|null;portfolio_limit_exceeded?:boolean};
type Plan={slug:string;name:string;portfolio_up_to_usd:number;monthly_usd:number;yearly_usd:number;yearly_monthly_equivalent_usd:number;description:string;monthly_checkout_available:boolean;yearly_checkout_available:boolean};
type Trial={days:number;portfolio_up_to_usd:number;max_positions:number;max_notional_per_trade_usd:number;max_multiplier:number;card_required:boolean};
type Catalog={currency:string;yearly_discount_pct:number;minimum_portfolio_usd:number;trial:Trial;plans:Plan[];overage:{included_portfolio_usd:number;excess_fee_annual_pct:number;custom_terms_from_usd:number};legacy_display_map:Record<string,string>};
type DiscountMap={discounts:Record<string,number>};
type Period='monthly'|'yearly';

const usd=(n:number,decimals=0)=>new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',minimumFractionDigits:decimals,maximumFractionDigits:decimals}).format(n);
const localDate=(value:string|null)=>value?new Intl.DateTimeFormat(htmlLocaleByLanguage[detectLanguage()],{day:'2-digit',month:'2-digit',year:'numeric'}).format(new Date(value)):null;
const discounted=(value:number,percent:number)=>Math.round(value*(100-percent))/100;
const moneyDecimals=(value:number)=>Number.isInteger(value)?0:2;

export default function Billing(){
  const [s,setS]=useState<Sub|null>(null);
  const [catalog,setCatalog]=useState<Catalog|null>(null);
  const [discounts,setDiscounts]=useState<Record<string,number>>({});
  const [period,setPeriod]=useState<Period>('monthly');
  const [busy,setBusy]=useState('');
  const [error,setError]=useState('');

  useEffect(()=>{
    void Promise.all([
      get<Sub>('/subscription'),
      get<Catalog>('/subscription/plans'),
      get<DiscountMap>('/subscription/discounts'),
    ]).then(([sub,plans,personal])=>{
      setS(sub);setCatalog(plans);setDiscounts(personal.discounts||{});
    }).catch(e=>setError(e instanceof Error?e.message:'Errore caricamento piani'));
  },[]);

  const current=useMemo(()=>s?.commercial_plan||s?.plan||null,[s]);
  const checkout=async(plan:string)=>{
    setError('');setBusy(plan);
    try{
      const r=await post<{url:string;personal_discount_pct:number}>('/subscription/checkout',{plan,billing_period:period});
      location.href=r.url;
    }catch(e){
      setError(e instanceof Error?e.message:'Checkout non disponibile');
    }finally{
      setBusy('');
    }
  };
  const activateComplimentary=async(plan:string)=>{
    setError('');setBusy(plan);
    try{
      const next=await post<Sub>('/subscription/activate-complimentary',{plan});
      setS(next);
    }catch(e){
      setError(e instanceof Error?e.message:'Attivazione piano non disponibile');
    }finally{
      setBusy('');
    }
  };
  const portal=async()=>{
    setError('');
    try{const r=await post<{url:string}>('/subscription/portal');location.href=r.url}
    catch(e){setError(e instanceof Error?e.message:'Customer Portal non disponibile')}
  };

  const trial=catalog?.trial;
  return <>
    <div className="title"><div><h1>Piani TRAXION</h1><p>Il piano dipende dal valore del portafoglio operativo. Tutti i limiti sono applicati anche lato backend.</p></div>{s?.plan&&s.plan!=='trial'&&s.status!=='complimentary'&&<button onClick={()=>void portal()}>Customer Portal</button>}</div>
    {error&&<div className="alert error billing-alert">{error}</div>}
    {trial&&<section className={`trial-offer ${current==='trial'?'active':''}`}><div><div className="trial-kicker">PROVA GRATUITA</div><h2>{trial.days} giorni per provare TRAXION</h2><p>Accesso completo a Dashboard, Risk Engine Basic + Pro e trading ibrido automatizzato su Hyperliquid, entro limiti pensati per una prova reale ma controllata.</p><div className="trial-features"><span>Portfolio fino a {usd(trial.portfolio_up_to_usd)}</span><span>Max {trial.max_positions} posizioni</span><span>Max {usd(trial.max_notional_per_trade_usd)} / trade</span><span>Intensità fino a {trial.max_multiplier}×</span><span>{trial.card_required?'Carta richiesta':'Nessuna carta richiesta'}</span></div></div><div className="trial-status"><strong>{current==='trial'?'TRIAL ATTIVO':'GRATIS'}</strong><span>{current==='trial'&&s?.period_end?`Termina il ${localDate(s.period_end)}`:'Si attiva automaticamente al primo accesso'}</span></div></section>}
    <section className="panel billing-current"><div><span>Piano attuale</span><strong>{current==='starter'?'Starter':current==='plus'?'Plus':current==='pro_10k'?'Pro':s?.plan||'—'}</strong></div><div><span>Stato</span><strong>{s?.status==='complimentary'?'Gratuito':s?.status||'—'}</strong></div><div><span>Entitlement</span><strong className={s?.entitled?'up':'down'}>{s?.entitled?'ATTIVO':'NON ATTIVO'}</strong></div>{s?.portfolio_equity!=null&&<div><span>Portafoglio rilevato</span><strong>{usd(s.portfolio_equity,2)}</strong></div>}</section>
    <div className="billing-cycle"><button className={period==='monthly'?'active':''} onClick={()=>setPeriod('monthly')}>Mensile</button><button className={period==='yearly'?'active':''} onClick={()=>setPeriod('yearly')}>Annuale</button><span>Risparmia {catalog?.yearly_discount_pct??50}%</span></div>
    <div className="pricing-grid">{catalog?.plans.map(p=>{
      const annual=period==='yearly';
      const baseMonthlyEquivalent=annual?p.yearly_monthly_equivalent_usd:p.monthly_usd;
      const baseBilled=annual?p.yearly_usd:p.monthly_usd;
      const personalDiscount=Math.max(0,Math.min(discounts[p.slug]??0,100));
      const complimentary=personalDiscount===100;
      const value=discounted(baseMonthlyEquivalent,personalDiscount);
      const billed=discounted(baseBilled,personalDiscount);
      const configured=annual?p.yearly_checkout_available:p.monthly_checkout_available;
      const selectable=complimentary||configured;
      const isCurrent=current===p.slug&&Boolean(s?.entitled);
      return <section className={`pricing-card ${p.slug==='plus'?'featured':''}`} key={p.slug}>
        <div className="pricing-cap">PORTAFOGLIO FINO A {usd(p.portfolio_up_to_usd)}</div>
        <h2>{p.name}</h2>
        <p className="pricing-description">{p.description}</p>
        {personalDiscount>0&&<div className="badge live" style={{width:'fit-content',marginBottom:10}}>Sconto personale {personalDiscount}%</div>}
        <div className="pricing-price">
          {personalDiscount>0&&<span style={{textDecoration:'line-through',opacity:.55,marginRight:8}}>{usd(baseMonthlyEquivalent,moneyDecimals(baseMonthlyEquivalent))}</span>}
          <strong>{usd(value,moneyDecimals(value))}</strong><span>/mese</span>
        </div>
        <div className="pricing-billed">{complimentary?'Piano gratuito con sconto personale 100% · nessun checkout richiesto':`${annual?`fatturato ${usd(billed,moneyDecimals(billed))}/anno`:'fatturato mensilmente'}${personalDiscount>0?' · sconto applicato automaticamente al checkout':''}`}</div>
        <ul className="pricing-features"><li>Trading ibrido multi-asset su Hyperliquid</li><li>Strategia sorgente sviluppata con analisti + sistemi AI</li><li>Risk Engine Basic + Pro</li><li>Esecuzione automatizzata, PnL e reconciliation</li></ul>
        <button className="pricing-cta" disabled={isCurrent||!selectable||busy===p.slug} title={!selectable?'Configura il relativo Stripe Price ID per abilitare il checkout':''} onClick={()=>void (complimentary?activateComplimentary(p.slug):checkout(p.slug))}>{isCurrent?'Piano attuale':busy===p.slug?(complimentary?'Attivazione…':'Apertura checkout…'):complimentary?'Attiva piano':configured?'Scegli piano':'Checkout da configurare'}</button>
      </section>})}</div>
    {catalog&&<div className="pricing-extras"><section className="panel"><h2>Portafoglio oltre {usd(catalog.overage.included_portfolio_usd)}?</h2><p>Rimani sul piano Pro e si applica una fee annua del <b>{catalog.overage.excess_fee_annual_pct}%</b> esclusivamente sul valore di portafoglio oltre {usd(catalog.overage.included_portfolio_usd)}.</p><small>La componente di eccedenza richiede il billing di produzione dedicato prima del lancio mainnet.</small></section><section className="panel"><h2>Portafoglio oltre {usd(catalog.overage.custom_terms_from_usd)}?</h2><p>Condizioni personalizzate, fee ridotte e supporto dedicato.</p><small>Contatto commerciale dedicato.</small></section></div>}
    <p className="pricing-footnote">Prezzi in USD. Portafoglio minimo indicativo: {usd(catalog?.minimum_portfolio_usd??500)}. Gli eventuali sconti personali sono associati al tuo account TRAXION e applicati server-side. Con sconto personale 100% il piano si attiva direttamente senza Stripe. In TESTNET/SUPERADMIN i limiti commerciali possono essere bypassati esclusivamente per validare la pipeline.</p>
  </>;
}
