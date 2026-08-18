import {createContext,useCallback,useContext,useEffect,useMemo,useState,type ReactNode} from 'react';
import {ApiError,get,post,setCsrf,type Session,type SessionUser} from './api';

type Ctx={user:SessionUser|null; entitlements:Record<string,unknown>; loading:boolean; error:string|null; signIn:()=>Promise<void>; signOut:()=>Promise<void>; refresh:()=>Promise<void>};
const Auth=createContext<Ctx|null>(null);

export function AuthProvider({children}:{children:ReactNode}){
 const [user,setUser]=useState<SessionUser|null>(null),[entitlements,setEnt]=useState<Record<string,unknown>>({}),[loading,setLoading]=useState(true),[error,setError]=useState<string|null>(null);
 const refresh=useCallback(async()=>{setError(null);try{const s=await get<Session>('/auth/session');setCsrf(s.csrf_token);setUser(s.user);setEnt(s.entitlements);}catch(e){setUser(null);setEnt({});if(e instanceof ApiError&&e.status===401){setError(null);}else{setError(e instanceof Error?e.message:'Impossibile contattare HyperCopy.');}}finally{setLoading(false);}},[]);
 useEffect(()=>{void refresh();},[refresh]);
 const signIn=useCallback(async()=>{setError(null);const w=window.ethereum;if(!w){setError('Installa MetaMask o un wallet EIP-1193 compatibile.');return;}setLoading(true);try{const accounts=await w.request({method:'eth_requestAccounts'}) as string[];const address=accounts?.[0];if(!address) throw new Error('Wallet non disponibile');const c=await post<{message:string}>('/auth/challenge',{address});const signature=await w.request({method:'personal_sign',params:[c.message,address]}) as string;const s=await post<Session>('/auth/verify',{address,signature});setCsrf(s.csrf_token);setUser(s.user);setEnt(s.entitlements);}catch(e){setError(e instanceof Error?e.message:'Accesso non riuscito');}finally{setLoading(false);}},[]);
 const signOut=useCallback(async()=>{try{await post<void>('/auth/logout');}catch{}setCsrf('');setUser(null);},[]);
 const value=useMemo(()=>({user,entitlements,loading,error,signIn,signOut,refresh}),[user,entitlements,loading,error,signIn,signOut,refresh]);
 return <Auth.Provider value={value}>{children}</Auth.Provider>;
}
export function useAuth(){const c=useContext(Auth);if(!c)throw new Error('AuthProvider missing');return c;}
