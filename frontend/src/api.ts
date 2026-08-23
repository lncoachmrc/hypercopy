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
export const SESSION_EXPIRED_EVENT='traxion:session-expired';
const authChannel=typeof BroadcastChannel!=='undefined'?new BroadcastChannel('traxion-auth-v1'):null;

authChannel?.addEventListener('message',(event:MessageEvent<{type?:string;csrf?:string}>)=>{
  if(event.data?.type==='csrf'&&typeof event.data.csrf==='string')csrf=event.data.csrf;
  if(event.data?.type==='expired'){
    csrf='';
    window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
  }
});

// CSRF is not an authentication credential, but keeping it only in memory and
// broadcasting fresh values avoids localStorage persistence while synchronizing
// tabs after another tab rotates the shared HttpOnly session cookies.
export const setCsrf = (value:string) => {
  csrf=value;
  if(value)authChannel?.postMessage({type:'csrf',csrf:value});
};

export class ApiError extends Error { constructor(public status:number,message:string,public code?:string){super(message);} }

const API_TIMEOUT_MS = 15_000;
export const ACTIVATION_TIMEOUT_MS = 60_000;
let refreshFlight:Promise<boolean>|null=null;

function requestHeaders(init:RequestInit){
  const method=(init.method||'GET').toUpperCase();
  const headers=new Headers(init.headers);
  if (init.body) headers.set('Content-Type','application/json');
  if (!['GET','HEAD','OPTIONS'].includes(method)) {
    headers.set('X-Requested-With','HyperCopy');
    if (csrf) headers.set('X-CSRF-Token',csrf);
  }
  return headers;
}

async function request(path:string,init:RequestInit,timeoutMs:number):Promise<Response>{
  const controller=new AbortController();
  const timer=window.setTimeout(()=>controller.abort(),timeoutMs);
  const externalSignal=init.signal;
  if(externalSignal){
    if(externalSignal.aborted)controller.abort();
    else externalSignal.addEventListener('abort',()=>controller.abort(),{once:true});
  }
  try{
    return await fetch(`${base()}${path}`,{...init,headers:requestHeaders(init),credentials:'include',signal:controller.signal});
  }catch(e){
    if(controller.signal.aborted)throw new ApiError(0,`Il server TRAXION non ha risposto entro ${Math.round(timeoutMs/1000)} secondi. Riprova o verifica lo stato dei servizi.`,'API_TIMEOUT');
    throw e;
  }finally{
    window.clearTimeout(timer);
  }
}

async function errorFrom(res:Response):Promise<ApiError>{
  let message=`Request failed (${res.status})`,code='';
  try{const body=await res.json();message=body?.error?.message||body?.detail||message;code=body?.error?.code||'';}catch{}
  return new ApiError(res.status,String(message),code);
}

async function isCsrfFailure(res:Response):Promise<boolean>{
  if(res.status!==403)return false;
  try{
    const body=await res.clone().json();
    const message=String(body?.error?.message||body?.detail||'').toLowerCase();
    return message.includes('csrf protection failed');
  }catch{return false;}
}

async function syncSessionFromAnotherTab(delayMs=180):Promise<boolean>{
  // Shared cookies may already have changed in another tab. Re-reading the
  // current session synchronizes this tab's in-memory CSRF without any wallet UI.
  if(delayMs>0)await new Promise(resolve=>window.setTimeout(resolve,delayMs));
  try{
    const res=await request('/auth/session',{method:'GET'},API_TIMEOUT_MS);
    if(!res.ok)return false;
    const session=await res.json() as Session;
    setCsrf(session.csrf_token);
    return true;
  }catch{return false;}
}

async function tryRefreshOnce():Promise<boolean>{
  const res=await request('/auth/refresh',{method:'POST'},API_TIMEOUT_MS);
  if(!res.ok)return false;
  const session=await res.json() as Session;
  setCsrf(session.csrf_token);
  return true;
}

async function renewSession():Promise<boolean>{
  if(refreshFlight)return refreshFlight;
  refreshFlight=(async()=>{
    // A response may be lost after the server has already rotated the cookie.
    // The backend keeps the predecessor as a bounded idempotency handle, so one
    // immediate retry can recover the exact same successor without a signature.
    for(let attempt=0;attempt<2;attempt+=1){
      try{
        if(await tryRefreshOnce())return true;
      }catch{}
      if(attempt===0)await new Promise(resolve=>window.setTimeout(resolve,180));
    }
    return await syncSessionFromAnotherTab(0);
  })();
  try{return await refreshFlight;}finally{refreshFlight=null;}
}

function canRefresh(path:string){
  return !['/auth/challenge','/auth/verify','/auth/refresh'].includes(path);
}

function announceExpiredSession(){
  csrf='';
  authChannel?.postMessage({type:'expired'});
  window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
}

export async function api<T>(path:string, init:RequestInit={}, timeoutMs=API_TIMEOUT_MS):Promise<T>{
  let res=await request(path,init,timeoutMs);
  if(res.status===401&&canRefresh(path)){
    const renewed=await renewSession();
    if(renewed)res=await request(path,init,timeoutMs);
    if(res.status===401)announceExpiredSession();
  }

  // Another tab may have rotated both shared cookies while this tab retained an
  // older CSRF value in memory. Recover only the explicit CSRF mismatch, then
  // repeat the original mutation once with the authoritative current token.
  if(await isCsrfFailure(res)){
    const synced=await syncSessionFromAnotherTab(0);
    if(synced)res=await request(path,init,timeoutMs);
  }

  if(!res.ok)throw await errorFrom(res);
  if(res.status===204)return undefined as T;
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
