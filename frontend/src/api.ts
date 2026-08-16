export type SessionUser = { id:string; auth_wallet:string; role:'USER'|'ADMIN'|'SUPERADMIN'; state:string; copy_state:string };
export type Session = { user: SessionUser; entitlements: Record<string,unknown>; csrf_token:string };

const cfg = () => window.__HYPERCOPY_CONFIG__ ?? {};
const base = () => (cfg().API_BASE_URL || '/api/v1').replace(/\/$/, '');
let csrf = '';
export const setCsrf = (value:string) => { csrf=value; };

export class ApiError extends Error { constructor(public status:number,message:string,public code?:string){super(message);} }

export async function api<T>(path:string, init:RequestInit={}):Promise<T>{
  const method=(init.method||'GET').toUpperCase();
  const headers=new Headers(init.headers);
  if (init.body) headers.set('Content-Type','application/json');
  if (!['GET','HEAD','OPTIONS'].includes(method)) {
    headers.set('X-Requested-With','HyperCopy');
    if (csrf) headers.set('X-CSRF-Token',csrf);
  }
  const res=await fetch(`${base()}${path}`,{...init,headers,credentials:'include'});
  if (!res.ok){
    let message=`Request failed (${res.status})`, code='';
    try { const body=await res.json(); message=body?.error?.message||body?.detail||message; code=body?.error?.code||''; } catch {}
    throw new ApiError(res.status,String(message),code);
  }
  if(res.status===204) return undefined as T;
  return res.json() as Promise<T>;
}
export const get=<T>(p:string)=>api<T>(p);
export const post=<T>(p:string,b?:unknown)=>api<T>(p,{method:'POST',body:b===undefined?undefined:JSON.stringify(b)});
export const put=<T>(p:string,b:unknown)=>api<T>(p,{method:'PUT',body:JSON.stringify(b)});
export const del=<T>(p:string)=>api<T>(p,{method:'DELETE'});

export function wsUrl(){
  if(cfg().WS_URL) return cfg().WS_URL!;
  const apiBase=base();
  if(apiBase.startsWith('http')) return apiBase.replace(/^http/,'ws').replace(/\/api\/v1$/,'')+'/api/v1/ws/events';
  return `${location.protocol==='https:'?'wss':'ws'}://${location.host}/api/v1/ws/events`;
}
