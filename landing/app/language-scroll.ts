type LanguageViewportSnapshot={
  top:boolean;
  sectionId:string|null;
  progress:number;
  absoluteY:number;
  hash:string;
  capturedAt:number;
};

const VIEWPORT_STORAGE_KEY="traxion_language_viewport_v1";
const MAX_SNAPSHOT_AGE_MS=15_000;
const TOP_THRESHOLD_PX=48;

function clamp(value:number,min:number,max:number){
  return Math.min(max,Math.max(min,value));
}

function viewportMarker(){
  if(typeof document==="undefined"||typeof window==="undefined")return 0;
  const header=document.querySelector<HTMLElement>(".site-header");
  const headerHeight=header?.getBoundingClientRect().height??0;
  return Math.min(window.innerHeight*.25,Math.max(24,headerHeight+16));
}

function landmarks(){
  if(typeof document==="undefined")return [] as HTMLElement[];
  const unique=new Map<string,HTMLElement>();
  document.querySelectorAll<HTMLElement>("main section[id], #master-performance").forEach(element=>{
    if(element.id&&element.getBoundingClientRect().height>0)unique.set(element.id,element);
  });
  return [...unique.values()];
}

function currentLandmark(marker:number){
  const elements=landmarks();
  if(!elements.length)return null;

  const containing=elements.find(element=>{
    const rect=element.getBoundingClientRect();
    return rect.top<=marker&&rect.bottom>marker;
  });
  if(containing)return containing;

  return elements.reduce<HTMLElement|null>((best,element)=>{
    if(!best)return element;
    const bestDistance=Math.abs(best.getBoundingClientRect().top-marker);
    const nextDistance=Math.abs(element.getBoundingClientRect().top-marker);
    return nextDistance<bestDistance?element:best;
  },null);
}

function storeSnapshot(snapshot:LanguageViewportSnapshot){
  if(typeof window==="undefined")return false;
  try{
    window.sessionStorage.setItem(VIEWPORT_STORAGE_KEY,JSON.stringify(snapshot));
    return true;
  }catch{
    return false;
  }
}

function takeSnapshot():LanguageViewportSnapshot|null{
  if(typeof window==="undefined")return null;
  try{
    const raw=window.sessionStorage.getItem(VIEWPORT_STORAGE_KEY);
    window.sessionStorage.removeItem(VIEWPORT_STORAGE_KEY);
    if(!raw)return null;
    const parsed=JSON.parse(raw) as LanguageViewportSnapshot;
    if(!parsed||Date.now()-parsed.capturedAt>MAX_SNAPSHOT_AGE_MS)return null;
    return parsed;
  }catch{
    return null;
  }
}

export function captureLanguageViewport(){
  if(typeof window==="undefined"||typeof document==="undefined")return false;

  const marker=viewportMarker();
  const top=window.scrollY<=TOP_THRESHOLD_PX;
  const landmark=top?null:currentLandmark(marker);
  const rect=landmark?.getBoundingClientRect();
  const progress=rect&&rect.height>0?clamp((marker-rect.top)/rect.height,0,1):0;
  const snapshot:LanguageViewportSnapshot={
    top,
    sectionId:landmark?.id??null,
    progress,
    absoluteY:window.scrollY,
    hash:window.location.hash,
    capturedAt:Date.now(),
  };

  const stored=storeSnapshot(snapshot);
  if(stored&&"scrollRestoration" in window.history)window.history.scrollRestoration="manual";
  return stored;
}

function targetScroll(snapshot:LanguageViewportSnapshot){
  if(snapshot.top)return 0;

  const marker=viewportMarker();
  if(snapshot.sectionId){
    const element=document.getElementById(snapshot.sectionId);
    if(element){
      const rect=element.getBoundingClientRect();
      const absoluteTop=window.scrollY+rect.top;
      return absoluteTop+rect.height*clamp(snapshot.progress,0,1)-marker;
    }
  }
  return snapshot.absoluteY;
}

function restoreOnce(snapshot:LanguageViewportSnapshot){
  const maxScroll=Math.max(0,document.documentElement.scrollHeight-window.innerHeight);
  const next=clamp(targetScroll(snapshot),0,maxScroll);
  if(Math.abs(window.scrollY-next)>1)window.scrollTo({top:next,left:0,behavior:"auto"});
}

function restoreHashWithoutScrolling(hash:string){
  if(!hash)return;
  const url=new URL(window.location.href);
  url.hash=hash;
  window.history.replaceState(window.history.state,"",`${url.pathname}${url.search}${url.hash}`);
}

export function restoreLanguageViewport(){
  if(typeof window==="undefined"||typeof document==="undefined")return()=>{};
  const snapshot=takeSnapshot();
  if(!snapshot)return()=>{};

  if("scrollRestoration" in window.history)window.history.scrollRestoration="manual";

  let frame=0;
  const timers:number[]=[];
  const restore=()=>restoreOnce(snapshot);

  frame=window.requestAnimationFrame(restore);
  timers.push(window.setTimeout(restore,160));
  timers.push(window.setTimeout(restore,520));
  timers.push(window.setTimeout(()=>{
    restore();
    restoreHashWithoutScrolling(snapshot.hash);
    if("scrollRestoration" in window.history)window.history.scrollRestoration="auto";
  },1000));

  return()=>{
    window.cancelAnimationFrame(frame);
    timers.forEach(timer=>window.clearTimeout(timer));
    if("scrollRestoration" in window.history)window.history.scrollRestoration="auto";
  };
}
