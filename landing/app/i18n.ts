import {captureLanguageViewport} from "./language-scroll";

export type Language="it"|"en"|"es";
export type LanguageSelection=Language|"auto";

export const DEFAULT_LANGUAGE:Language="it";
const UNSUPPORTED_BROWSER_FALLBACK:Language="en";
const STORAGE_KEY="traxion_language";
export const SUPPORTED_LANGUAGES:Language[]=["it","en","es"];

export const languageLabels:Record<Language,string>={it:"Italiano",en:"English",es:"Español"};
export const htmlLocaleByLanguage:Record<Language,string>={it:"it-IT",en:"en-GB",es:"es-ES"};

export function normaliseLanguage(value?:string|null):Language|null{
  if(!value)return null;
  const code=value.toLowerCase().split("-")[0];
  return SUPPORTED_LANGUAGES.includes(code as Language)?code as Language:null;
}

function browserLanguage():Language{
  if(typeof window==="undefined")return DEFAULT_LANGUAGE;
  for(const candidate of window.navigator.languages||[window.navigator.language]){
    const language=normaliseLanguage(candidate);
    if(language)return language;
  }
  return UNSUPPORTED_BROWSER_FALLBACK;
}

function querySelection():LanguageSelection|null{
  if(typeof window==="undefined")return null;
  const raw=new URLSearchParams(window.location.search).get("lang");
  if(raw?.toLowerCase()==="auto")return "auto";
  return normaliseLanguage(raw);
}

function readStoredLanguage():string|null{
  if(typeof window==="undefined")return null;
  try{return window.localStorage.getItem(STORAGE_KEY);}catch{return null;}
}

function writeStoredLanguage(value:Language):boolean{
  if(typeof window==="undefined")return false;
  try{window.localStorage.setItem(STORAGE_KEY,value);return true;}catch{return false;}
}

function clearStoredLanguage():boolean{
  if(typeof window==="undefined")return false;
  try{window.localStorage.removeItem(STORAGE_KEY);return true;}catch{return false;}
}

export function getStoredLanguage():Language|null{
  return normaliseLanguage(readStoredLanguage());
}

export function detectLanguage():Language{
  if(typeof window==="undefined")return DEFAULT_LANGUAGE;
  const query=querySelection();
  if(query==="auto")return browserLanguage();
  if(query)return query;
  return getStoredLanguage()||browserLanguage();
}

export function initLanguage():Language{
  const language=detectLanguage();
  if(typeof document!=="undefined")document.documentElement.lang=htmlLocaleByLanguage[language];
  if(typeof window!=="undefined"){
    const query=querySelection();
    let persisted=true;
    if(query==="auto")persisted=clearStoredLanguage();
    else if(query)persisted=writeStoredLanguage(language);

    // The query parameter is the authoritative hand-off between navigations.
    // Remove it only after the preference has been persisted successfully.
    // If storage is blocked, keeping ?lang= preserves the user's explicit choice.
    if(query&&persisted){
      const url=new URL(window.location.href);
      url.searchParams.delete("lang");
      window.history.replaceState(window.history.state,"",`${url.pathname}${url.search}${url.hash}`);
    }
  }
  return language;
}

export function persistLanguageSelection(selection:LanguageSelection):boolean{
  return selection==="auto"?clearStoredLanguage():writeStoredLanguage(selection);
}

export function languageNavigationUrl(selection:LanguageSelection,currentHref:string,preserveHash=true){
  const url=new URL(currentHref);
  // Always change the URL when the user changes language. A captured viewport is
  // restored after reload, so suppress the old hash during navigation to prevent
  // the browser from jumping to the section start before the layout has settled.
  url.searchParams.set("lang",selection);
  if(!preserveHash)url.hash="";
  return `${url.pathname}${url.search}${url.hash}`;
}

export function changeLanguage(selection:LanguageSelection){
  if(typeof window==="undefined")return;
  const viewportCaptured=captureLanguageViewport();
  persistLanguageSelection(selection);
  window.location.assign(languageNavigationUrl(selection,window.location.href,!viewportCaptured));
}

export function tr(it:string,en:string,es:string){
  const language=detectLanguage();
  if(language==="en")return en||it||es;
  if(language==="es")return es||it||en;
  return it||en||es;
}

export function withLanguageQuery(value:string){
  if(typeof window==="undefined")return value;
  const url=new URL(value,window.location.href);
  url.searchParams.set("lang",detectLanguage());
  return url.toString();
}
