import {createContext,useCallback,useContext,useEffect,useMemo,useRef,useState,type ReactNode} from 'react';
import {ApiError,get,post,setCsrf,type Session,type SessionUser} from './api';

type SignMessage=(message:string)=>Promise<string>;
type Ctx={user:SessionUser|null;entitlements:Record<string,unknown>;loading:boolean;ready:boolean;error:string|null;signIn:(address:string,signMessage:SignMessage)=>Promise<void>;signOut:()=>Promise<void>;refresh:()=>Promise<void>;clearError:()=>void};
const Auth=createContext<Ctx|null>(null);

export class AuthFlowError extends Error{constructor(message:string){super(message);this.name='AuthFlowError';}}

function authErrorMessage(error:unknown){
 if(typeof navigator!=='undefined'&&!navigator.onLine)return 'Sei offline. Ripristina la connessione e riprova.';
 if(error instanceof ApiError){
  const message=error.message.toLowerCase();
  if(error.code==='API_TIMEOUT')return 'Il server TRAXION non ha risposto in tempo. Riprova.';
  if(error.status===401&&message.includes('challenge expired'))return 'Il collegamento è scaduto. Riprova per generare una nuova richiesta.';
  if(error.status===401)return 'La firma non è valida o la richiesta è scaduta. Riprova.';
  if(error.status===422)return 'L’indirizzo del wallet non è valido.';
  if(error.status===429)return 'Troppe richieste di accesso. Attendi qualche istante e riprova.';
  if(error.status>=500)return 'Il servizio di accesso TRAXION è temporaneamente non disponibile. Riprova.';
  return 'Accesso non riuscito. Riprova.';
 }
 if(error instanceof AuthFlowError)return error.message;
 if(error instanceof Error&&error.message)return error.message;
 return 'Accesso non riuscito. Riprova.';
}

export function AuthProvider({children}:{children:ReactNode}){
 const [user,setUser]=useState<SessionUser|null>(null),[entitlements,setEnt]=useState<Record<string,unknown>>({}),[loading,setLoading]=useState(true),[ready,setReady]=useState(false),[error,setError]=useState<string|null>(null);
 const signInFlight=useRef(false);
 const applySession=useCallback((session:Session)=>{setCsrf(session.csrf_token);setUser(session.user);setEnt(session.entitlements);},[]);
 const clearError=useCallback(()=>setError(null),[]);
 const refresh=useCallback(async()=>{setLoading(true);setError(null);try{const session=await get<Session>('/auth/session');applySession(session);}catch(e){setCsrf('');setUser(null);setEnt({});if(e instanceof ApiError&&e.status===401){setError(null);}else{setError(e instanceof Error?e.message:'Impossibile contattare TRAXION.');}}finally{setLoading(false);setReady(true);}},[applySession]);
 useEffect(()=>{void refresh();},[refresh]);
 const signIn=useCallback(async(address:string,signMessage:SignMessage)=>{
  if(signInFlight.current)return;
  signInFlight.current=true;setError(null);setLoading(true);
  try{
   const challenge=await post<{message:string}>('/auth/challenge',{address});
   const signature=await signMessage(challenge.message);
   const session=await post<Session>('/auth/verify',{address,signature});
   applySession(session);
  }catch(e){const message=authErrorMessage(e);setError(message);throw new AuthFlowError(message);}finally{signInFlight.current=false;setLoading(false);}
 },[applySession]);
 const signOut=useCallback(async()=>{try{await post<void>('/auth/logout');}catch{}setCsrf('');setUser(null);setEnt({});},[]);
 const value=useMemo(()=>({user,entitlements,loading,ready,error,signIn,signOut,refresh,clearError}),[user,entitlements,loading,ready,error,signIn,signOut,refresh,clearError]);
 return <Auth.Provider value={value}>{children}</Auth.Provider>;
}
export function useAuth(){const c=useContext(Auth);if(!c)throw new Error('AuthProvider missing');return c;}
