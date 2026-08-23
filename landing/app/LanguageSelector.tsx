"use client";

import {useEffect,useRef,useState} from "react";
import {initAutoTranslate} from "./autoTranslate";
import {changeLanguage,detectLanguage,initLanguage,languageLabels,SUPPORTED_LANGUAGES,tr,type Language} from "./i18n";

function GlobeIcon(){return <svg aria-hidden="true" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.4 2.5 3.6 5.5 3.6 9S14.4 18.5 12 21c-2.4-2.5-3.6-5.5-3.6-9S9.6 5.5 12 3Z"/></svg>}

export function LanguageController(){
  useEffect(()=>{
    const language=initLanguage();
    if(language==="en"){
      document.title="TRAXION | Hybrid intelligence. Deterministic execution.";
      document.querySelector('meta[name="description"]')?.setAttribute("content","TRAXION connects human analysis, Capital Intelligence AI, Risk Engine and disciplined execution on Hyperliquid.");
    }else if(language==="es"){
      document.title="TRAXION | Inteligencia híbrida. Ejecución determinista.";
      document.querySelector('meta[name="description"]')?.setAttribute("content","TRAXION conecta análisis humano, Capital Intelligence AI, Risk Engine y ejecución disciplinada en Hyperliquid.");
    }
    return initAutoTranslate();
  },[]);
  return null;
}

export default function LanguageSelector(){
  const [open,setOpen]=useState(false);
  const [language,setLanguage]=useState<Language>("it");
  const ref=useRef<HTMLDivElement>(null);
  useEffect(()=>setLanguage(detectLanguage()),[]);
  useEffect(()=>{
    if(!open)return;
    const outside=(event:PointerEvent)=>{if(!ref.current?.contains(event.target as Node))setOpen(false)};
    const key=(event:KeyboardEvent)=>{if(event.key==="Escape")setOpen(false)};
    document.addEventListener("pointerdown",outside);
    document.addEventListener("keydown",key);
    return()=>{document.removeEventListener("pointerdown",outside);document.removeEventListener("keydown",key)};
  },[open]);

  return <div className="landing-language-selector" ref={ref} data-no-translate>
    <button type="button" className="landing-language-trigger" aria-haspopup="menu" aria-expanded={open} aria-label={tr("Seleziona lingua","Select language","Seleccionar idioma")} onClick={()=>setOpen(value=>!value)}>
      <GlobeIcon/><span>{language.toUpperCase()}</span><span aria-hidden="true">⌄</span>
    </button>
    {open&&<div className="landing-language-menu" role="menu" aria-label={tr("Lingue disponibili","Available languages","Idiomas disponibles")}>
      {SUPPORTED_LANGUAGES.map(option=><button type="button" key={option} role="menuitemradio" aria-checked={option===language} className={option===language?"active":""} onClick={()=>changeLanguage(option)}>
        <span>{option.toUpperCase()}</span><span>{languageLabels[option]}</span><span aria-hidden="true">{option===language?"✓":""}</span>
      </button>)}
    </div>}
  </div>;
}
