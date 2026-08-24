import {useEffect,useRef,useState} from 'react';
import {changeLanguage,detectLanguage,languageLabels,SUPPORTED_LANGUAGES,tr,type Language} from './i18n';

function GlobeIcon(){return <svg aria-hidden="true" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.8"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.4 2.5 3.6 5.5 3.6 9S14.4 18.5 12 21c-2.4-2.5-3.6-5.5-3.6-9S9.6 5.5 12 3Z"/></svg>}

export default function LanguageSelector({compact=false}:{compact?:boolean}){
  const [open,setOpen]=useState(false);
  const [language,setLanguage]=useState<Language>('it');
  const ref=useRef<HTMLDivElement>(null);

  useEffect(()=>setLanguage(detectLanguage()),[]);
  useEffect(()=>{
    if(!open)return;
    const outside=(event:PointerEvent)=>{if(!ref.current?.contains(event.target as Node))setOpen(false)};
    const key=(event:KeyboardEvent)=>{if(event.key==='Escape')setOpen(false)};
    document.addEventListener('pointerdown',outside);
    document.addEventListener('keydown',key);
    return()=>{document.removeEventListener('pointerdown',outside);document.removeEventListener('keydown',key)};
  },[open]);

  return <div className={`language-selector${compact?' compact':''}`} ref={ref} data-no-translate>
    <button type="button" className="language-trigger" aria-haspopup="menu" aria-expanded={open} aria-label={tr('Seleziona lingua','Select language','Seleccionar idioma')} onClick={()=>setOpen(value=>!value)}>
      <GlobeIcon/><span>{language.toUpperCase()}</span><span className="language-chevron" aria-hidden="true">⌄</span>
    </button>
    {open&&<div className="language-menu" role="menu" aria-label={tr('Lingue disponibili','Available languages','Idiomas disponibles')}>
      {SUPPORTED_LANGUAGES.map(option=><button key={option} type="button" role="menuitemradio" aria-checked={option===language} className={option===language?'active':''} onClick={()=>changeLanguage(option)}>
        <span className="language-code">{option.toUpperCase()}</span><span>{languageLabels[option]}</span><span aria-hidden="true">{option===language?'✓':''}</span>
      </button>)}
    </div>}
  </div>;
}
