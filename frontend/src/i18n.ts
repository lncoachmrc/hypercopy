export type Language='it'|'en'|'es';
export type LanguageSelection=Language|'auto';

export const DEFAULT_LANGUAGE:Language='it';
const UNSUPPORTED_BROWSER_FALLBACK:Language='en';
const STORAGE_KEY='traxion_language';
export const SUPPORTED_LANGUAGES:Language[]=['it','en','es'];

export const languageLabels:Record<Language,string>={
  it:'Italiano',
  en:'English',
  es:'Español',
};

export const htmlLocaleByLanguage:Record<Language,string>={
  it:'it-IT',
  en:'en-GB',
  es:'es-ES',
};

const documentCopy:Record<Language,{title:string;description:string}>={
  it:{title:'TRAXION | Trading ibrido su Hyperliquid con analisti + AI',description:'TRAXION: trading ibrido su Hyperliquid con analisti, sistemi AI, Risk Engine ed esecuzione automatizzata.'},
  en:{title:'TRAXION | Hybrid trading on Hyperliquid with analysts + AI',description:'TRAXION: hybrid trading on Hyperliquid with analysts, AI systems, Risk Engine and automated execution.'},
  es:{title:'TRAXION | Trading híbrido en Hyperliquid con analistas + IA',description:'TRAXION: trading híbrido en Hyperliquid con analistas, sistemas de IA, Risk Engine y ejecución automatizada.'},
};

export function normaliseLanguage(value?:string|null):Language|null{
  if(!value)return null;
  const code=value.toLowerCase().split('-')[0];
  return SUPPORTED_LANGUAGES.includes(code as Language)?code as Language:null;
}

function browserLanguage():Language{
  if(typeof window==='undefined')return DEFAULT_LANGUAGE;
  for(const candidate of window.navigator.languages||[window.navigator.language]){
    const language=normaliseLanguage(candidate);
    if(language)return language;
  }
  return UNSUPPORTED_BROWSER_FALLBACK;
}

function queryLanguage():LanguageSelection|null{
  if(typeof window==='undefined')return null;
  const raw=new URLSearchParams(window.location.search).get('lang');
  if(raw?.toLowerCase()==='auto')return 'auto';
  return normaliseLanguage(raw);
}

export function getStoredLanguage():Language|null{
  if(typeof window==='undefined')return null;
  return normaliseLanguage(window.localStorage.getItem(STORAGE_KEY));
}

export function detectLanguage():Language{
  if(typeof window==='undefined')return DEFAULT_LANGUAGE;
  const query=queryLanguage();
  if(query==='auto')return browserLanguage();
  if(query)return query;
  return getStoredLanguage()||browserLanguage();
}

export function initLanguage():Language{
  const language=detectLanguage();
  if(typeof document!=='undefined'){
    document.documentElement.lang=htmlLocaleByLanguage[language];
    document.title=documentCopy[language].title;
    document.querySelector('meta[name="description"]')?.setAttribute('content',documentCopy[language].description);
  }
  if(typeof window!=='undefined'){
    const query=queryLanguage();
    if(query&&query!=='auto')window.localStorage.setItem(STORAGE_KEY,language);
    if(query){
      const url=new URL(window.location.href);
      url.searchParams.delete('lang');
      window.history.replaceState(window.history.state,'',`${url.pathname}${url.search}${url.hash}`);
    }
  }
  return language;
}

export function persistLanguageSelection(selection:LanguageSelection){
  if(typeof window==='undefined')return;
  if(selection==='auto')window.localStorage.removeItem(STORAGE_KEY);
  else window.localStorage.setItem(STORAGE_KEY,selection);
}

export function changeLanguage(selection:LanguageSelection){
  if(typeof window==='undefined')return;
  persistLanguageSelection(selection);
  const url=new URL(window.location.href);
  url.searchParams.delete('lang');
  window.location.assign(`${url.pathname}${url.search}${url.hash}`);
}

export function tr(it:string,en:string,es:string){
  const language=detectLanguage();
  if(language==='en')return en||it||es;
  if(language==='es')return es||it||en;
  return it||en||es;
}

export function withLanguageQuery(value:string){
  if(typeof window==='undefined')return value;
  const url=new URL(value,window.location.href);
  url.searchParams.set('lang',detectLanguage());
  return url.toString();
}
