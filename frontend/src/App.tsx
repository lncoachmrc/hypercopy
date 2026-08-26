import {lazy,Suspense,useEffect,useState} from 'react';
import {Navigate,NavLink,Route,Routes} from 'react-router-dom';
import {useDisconnect} from '@reown/appkit/react';
import {useAuth} from './auth';
import {ApiError,get} from './api';
import Billing from './Billing';
import Admin from './Admin';
import AdminPlanDiscounts from './AdminPlanDiscounts';
import AiStatus,{AiModeToggle} from './AiStatus';
import LanguageSelector from './LanguageSelector';
import {MasterAwareDashboard,MasterAwareSettings} from './MasterSourceView';

const WalletLogin=lazy(()=>import('./WalletLogin'));

type MasterMode='loading'|'master'|'follower';

function useMasterMode(){
  const [mode,setMode]=useState<MasterMode>('loading');
  useEffect(()=>{
    let cancelled=false;
    let timer:number|undefined;
    let attempts=0;
    const load=async()=>{
      attempts+=1;
      try{
        await get('/master-source/status');
        if(!cancelled)setMode('master');
      }catch(error){
        if(cancelled)return;
        if(error instanceof ApiError&&error.status===403){setMode('follower');return;}
        if(attempts<3){timer=window.setTimeout(()=>void load(),1500*attempts);return;}
        // Only a confirmed master response disables commercial billing.
        // After transient/indeterminate probe failures, keep follower billing available.
        setMode('follower');
      }
    };
    void load();
    return()=>{cancelled=true;if(timer!==undefined)window.clearTimeout(timer)};
  },[]);
  return mode;
}

function BrandLogo({login=false}:{login?:boolean}){return <img className={`brand-logo${login?' brand-logo--login':''}`} src={login?'/traxion-logo-completo.webp':'/traxion-ai-copy-trading-logo.webp'} alt={login?'TRAXION — Hyperliquid AI Trading Agent':'TRAXION'} width={login?1672:2048} height={login?941:682} decoding="async"/>}
function Gate({children,admin=false}:{children:React.ReactNode;admin?:boolean}){const {user,loading,ready}=useAuth();if(!ready||loading)return <div className="center">Caricamento…</div>;if(!user)return <Navigate to="/login" replace/>;if(admin&&!['ADMIN','SUPERADMIN'].includes(user.role))return <Navigate to="/" replace/>;return <>{children}</>}
function Login(){const {user,ready}=useAuth();if(!ready)return <div className="center">Caricamento…</div>;if(user)return <Navigate to="/" replace/>;return <main className="login"><section className="panel loginbox"><LanguageSelector compact/><BrandLogo login/><h1>Trading ibrido. Analisti + AI. Esecuzione automatizzata.</h1><p>TRAXION applica sul tuo account Hyperliquid una strategia operativa sviluppata con analisi umana e sistemi di intelligenza artificiale, con Risk Engine e controlli verificabili.</p><p>Accedi firmando un messaggio con il tuo wallet. La firma è gratuita: nessuna transazione e nessun gas.</p><Suspense fallback={<button className="primary" disabled aria-busy="true">Preparazione wallet…</button>}><WalletLogin/></Suspense></section></main>}
function Shell(){const {user,signOut}=useAuth();const masterMode=useMasterMode();const {disconnect}=useDisconnect();const logout=async()=>{try{await disconnect();}catch{}await signOut();};const billingRoute=masterMode==='master'?<Navigate to="/" replace/>:masterMode==='loading'?<div className="center">Verifica profilo…</div>:<Billing/>;return <div><header><BrandLogo/><nav><NavLink to="/">Dashboard</NavLink><NavLink to="/settings">Configurazione</NavLink>{masterMode==='follower'&&<NavLink to="/billing">Piano</NavLink>}{user&&['ADMIN','SUPERADMIN'].includes(user.role)&&<NavLink to="/admin">Control room</NavLink>}</nav><div style={{display:'flex',alignItems:'center',gap:10}}><LanguageSelector compact/><AiModeToggle/><AiStatus/><button onClick={()=>void logout()}>Esci</button></div></header><div className="wrap"><Routes><Route index element={<MasterAwareDashboard/>}/><Route path="settings" element={<MasterAwareSettings/>}/><Route path="billing" element={billingRoute}/><Route path="admin" element={<Gate admin><><Admin/><AdminPlanDiscounts/></></Gate>}/></Routes></div></div>}
export default function App(){return <Routes><Route path="/login" element={<Login/>}/><Route path="/*" element={<Gate><Shell/></Gate>}/></Routes>}