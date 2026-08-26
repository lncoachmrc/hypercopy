import {lazy,Suspense} from 'react';
import {Navigate,NavLink,Route,Routes} from 'react-router-dom';
import {useDisconnect} from '@reown/appkit/react';
import {useAuth} from './auth';
import Dashboard from './Dashboard';
import Settings from './Settings';
import Billing from './Billing';
import Admin from './Admin';
import AdminPlanDiscounts from './AdminPlanDiscounts';
import AiStatus,{AiModeToggle} from './AiStatus';
import LanguageSelector from './LanguageSelector';

const WalletLogin=lazy(()=>import('./WalletLogin'));

function BrandLogo({login=false}:{login?:boolean}){return <img className={`brand-logo${login?' brand-logo--login':''}`} src={login?'/traxion-logo-completo.webp':'/traxion-ai-copy-trading-logo.webp'} alt={login?'TRAXION — Hyperliquid AI Trading Agent':'TRAXION'} width={login?1672:2048} height={login?941:682} decoding="async"/>}
function Gate({children,admin=false}:{children:React.ReactNode;admin?:boolean}){const {user,loading,ready}=useAuth();if(!ready||loading)return <div className="center">Caricamento…</div>;if(!user)return <Navigate to="/login" replace/>;if(admin&&!['ADMIN','SUPERADMIN'].includes(user.role))return <Navigate to="/" replace/>;return <>{children}</>}
function Login(){const {user,ready}=useAuth();if(!ready)return <div className="center">Caricamento…</div>;if(user)return <Navigate to="/" replace/>;return <main className="login"><section className="panel loginbox"><LanguageSelector compact/><BrandLogo login/><h1>Trading ibrido. Analisti + AI. Esecuzione automatizzata.</h1><p>TRAXION applica sul tuo account Hyperliquid una strategia operativa sviluppata con analisi umana e sistemi di intelligenza artificiale, con Risk Engine e controlli verificabili.</p><p>Accedi firmando un messaggio con il tuo wallet. La firma è gratuita: nessuna transazione e nessun gas.</p><Suspense fallback={<button className="primary" disabled aria-busy="true">Preparazione wallet…</button>}><WalletLogin/></Suspense></section></main>}
function Shell(){const {user,signOut}=useAuth();const {disconnect}=useDisconnect();const logout=async()=>{try{await disconnect();}catch{}await signOut();};return <div><header><BrandLogo/><nav><NavLink to="/">Dashboard</NavLink><NavLink to="/settings">Configurazione</NavLink><NavLink to="/billing">Piano</NavLink>{user&&['ADMIN','SUPERADMIN'].includes(user.role)&&<NavLink to="/admin">Control room</NavLink>}</nav><div style={{display:'flex',alignItems:'center',gap:10}}><LanguageSelector compact/><AiModeToggle/><AiStatus/><button onClick={()=>void logout()}>Esci</button></div></header><div className="wrap"><Routes><Route index element={<Dashboard/>}/><Route path="settings" element={<Settings/>}/><Route path="billing" element={<Billing/>}/><Route path="admin" element={<Gate admin><><Admin/><AdminPlanDiscounts/></></Gate>}/></Routes></div></div>}
export default function App(){return <Routes><Route path="/login" element={<Login/>}/><Route path="/*" element={<Gate><Shell/></Gate>}/></Routes>}
