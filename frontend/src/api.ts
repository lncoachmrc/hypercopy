export type SessionUser = { id:string; auth_wallet:string; role:'USER'|'ADMIN'|'SUPERADMIN'; state:string; copy_state:string };
export type Session = { user: SessionUser; entitlements: Record<string,unknown>; csrf_token:string };

const cfg = () => window.__HYPERCOPY_CONFIG__ ?? {};

// Security/session invariant: deployed TRAXION browser traffic must stay on
// the frontend origin and reach the API through Nginx /api/v1. This prevents
// stale absolute Railway URLs from reintroducing cross-site cookie problems.
const isLocalDev = () => ['localhost','127.0.0.1','::1'].includes(location.hostname);
const base = () => {
  const configured=(cfg().API_BASE_URL || '').replace(/\/$/, '');
  if (isLocalDev() && configured) return configured;
  return '/api/v1';
};
let csrf = '';
export const setCsrf = (value:string) => { csrf=value; };

export class ApiError extends Error { constructor(public status:number,message:string,public code?:string){super(message);} }

const API_TIMEOUT_MS = 15_000;
export const ACTIVATION_TIMEOUT_MS = 60_000;

export async function api<T>(path:string, init:RequestInit={}, timeoutMs=API_TIMEOUT_MS):Promise<T>{
  const method=(init.method||'GET').toUpperCase();
  const headers=new Headers(init.headers);
  if (init.body) headers.set('Content-Type','application/json');
  if (!['GET','HEAD','OPTIONS'].includes(method)) {
    headers.set('X-Requested-With','HyperCopy');
    if (csrf) headers.set('X-CSRF-Token',csrf);
  }

  const controller = new AbortController();
  const timer = window.setTimeout(()=>controller.abort(), timeoutMs);
  const externalSignal = init.signal;
  if (externalSignal) {
    if (externalSignal.aborted) controller.abort();
    else externalSignal.addEventListener('abort',()=>controller.abort(),{once:true});
  }

  let res:Response;
  try {
    res=await fetch(`${base()}${path}`,{...init,headers,credentials:'include',signal:controller.signal});
  } catch (e) {
    if (controller.signal.aborted) throw new ApiError(0,`Il server TRAXION non ha risposto entro ${Math.round(timeoutMs/1000)} secondi. Riprova o verifica lo stato dei servizi.`,'API_TIMEOUT');
    throw e;
  } finally {
    window.clearTimeout(timer);
  }

  if (!res.ok){
    let message=`Request failed (${res.status})`, code='';
    try { const body=await res.json(); message=body?.error?.message||body?.detail||message; code=body?.error?.code||''; } catch {}
    throw new ApiError(res.status,String(message),code);
  }
  if(res.status===204) return undefined as T;
  return res.json() as Promise<T>;
}
export const get=<T>(p:string)=>api<T>(p);
export const post=<T>(p:string,b?:unknown,timeoutMs?:number)=>api<T>(p,{method:'POST',body:b===undefined?undefined:JSON.stringify(b)},timeoutMs);
export const put=<T>(p:string,b:unknown)=>api<T>(p,{method:'PUT',body:JSON.stringify(b)});
export const del=<T>(p:string)=>api<T>(p,{method:'DELETE'});

export function wsUrl(){
  if(isLocalDev() && cfg().WS_URL) return cfg().WS_URL!;
  const apiBase=base();
  if(apiBase.startsWith('http')) return apiBase.replace(/^http/,'ws').replace(/\/api\/v1$/,'')+'/api/v1/ws/events';
  return `${location.protocol==='https:'?'wss':'ws'}://${location.host}/api/v1/ws/events`;
}
