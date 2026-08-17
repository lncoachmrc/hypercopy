import {Navigate,NavLink,Route,Routes} from 'react-router-dom';
import {useAuth} from './auth';
import Dashboard from './Dashboard';
import Settings from './Settings';
import Billing from './Billing';
import Admin from './Admin';

function Gate({children,admin=false}:{children:React.ReactNode;admin?:boolean}){const {user,loading}=useAuth();if(loading)return <div className="center">Caricamento…</div>;if(!user)return <Navigate to="/login" replace/>;if(admin&&!['ADMIN','SUPERADMIN'].includes(user.role))return <Navigate to="/" replace/>;return <>{children}</>}
function Login(){const {user,signIn,error,loading}=useAuth();if(user)return <Navigate to="/" replace/>;return <main className="login"><section className="panel loginbox"><div className="brand">HYPERCOPY</div><h1>Trading ibrido. Analisti + AI. Esecuzione automatizzata.</h1><p>HyperCopy applica sul tuo account Hyperliquid una strategia operativa sviluppata con analisi umana e sistemi di intelligenza artificiale, con Risk Engine e controlli verificabili.</p><p>Accedi firmando un messaggio con il tuo wallet. Nessuna transazione e nessun gas.</p>{error&&<div className="alert error">{error}</div>}<button className="primary" disabled={loading} onClick={()=>void signIn()}>{loading?'Connessione…':'Connetti wallet'}</button></section></main>}
function Shell(){const {user,signOut}=useAuth();return <div><header><div className="brand">HYPERCOPY</div><nav><NavLink to="/">Dashboard</NavLink><NavLink to="/settings">Configurazione</NavLink><NavLink to="/billing">Piano</NavLink>{user&&['ADMIN','SUPERADMIN'].includes(user.role)&&<NavLink to="/admin">Control room</NavLink>}</nav><button onClick={()=>void signOut()}>Esci</button></header><div className="wrap"><Routes><Route index element={<Dashboard/>}/><Route path="settings" element={<Settings/>}/><Route path="billing" element={<Billing/>}/><Route path="admin" element={<Gate admin><Admin/></Gate>}/></Routes></div></div>}
export default function App(){return <Routes><Route path="/login" element={<Login/>}/><Route path="/*" element={<Gate><Shell/></Gate>}/></Routes>}
