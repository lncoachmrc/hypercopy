import {detectLanguage,type Language} from './i18n';
import {FRONTEND_DICTIONARIES,type TranslationMap} from './translations';
import {ADMIN_EN,ADMIN_ES} from './admin-translations';
import {AUTH_EN,AUTH_ES} from './auth-translations';
import {DISCOUNT_EN,DISCOUNT_ES} from './discount-translations';

const SKIP_TAGS=new Set(['SCRIPT','STYLE','NOSCRIPT','TEXTAREA','CODE','PRE']);
let cachedLanguage:Language|null=null;
let cachedEntries:[string,string][]=[];
const translatedText=new WeakMap<Node,string>();
const translatedAttributes=new WeakMap<Element,Map<string,string>>();

function activeDictionary(language:Language):TranslationMap{
  if(language==='it')return {};
  const base=FRONTEND_DICTIONARIES[language];
  const admin=language==='en'?ADMIN_EN:ADMIN_ES;
  const auth=language==='en'?AUTH_EN:AUTH_ES;
  const discounts=language==='en'?DISCOUNT_EN:DISCOUNT_ES;
  return {...base,...admin,...auth,...discounts};
}

function normalise(value:string){return value.replace(/\s+/g,' ').trim()}
function escapeRegExp(value:string){return value.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')}
function entries(){
  const language=detectLanguage();
  if(cachedLanguage!==language){
    cachedLanguage=language;
    cachedEntries=Object.entries(activeDictionary(language)).sort((a,b)=>b[0].length-a[0].length);
  }
  return cachedEntries;
}

export function translateText(value:string){
  const dictionaryEntries=entries();
  if(!dictionaryEntries.length)return value;
  const compact=normalise(value);
  const exact=dictionaryEntries.find(([source])=>normalise(source)===compact);
  if(exact){
    const prefix=value.match(/^\s*/)?.[0]||'';
    const suffix=value.match(/\s*$/)?.[0]||'';
    return `${prefix}${exact[1]}${suffix}`;
  }
  let output=value;
  for(const [source,target] of dictionaryEntries)output=output.replace(new RegExp(escapeRegExp(source),'g'),target);
  return output;
}

function skipped(element:Element|null){
  if(!element)return false;
  return SKIP_TAGS.has(element.tagName)||Boolean(element.closest('[data-no-translate]'));
}

function translateAttributes(element:Element){
  if(skipped(element))return;
  let lastValues=translatedAttributes.get(element);
  if(!lastValues){lastValues=new Map<string,string>();translatedAttributes.set(element,lastValues);}
  ['placeholder','aria-label','alt','title'].forEach(attribute=>{
    const value=element.getAttribute(attribute);
    if(!value||lastValues!.get(attribute)===value)return;
    const next=translateText(value);
    if(next!==value){
      lastValues!.set(attribute,next);
      element.setAttribute(attribute,next);
    }else{
      lastValues!.delete(attribute);
    }
  });
}

function translateNode(node:Node){
  if(node.nodeType!==Node.TEXT_NODE||!node.textContent?.trim()||skipped(node.parentElement))return;
  const current=node.textContent;
  if(translatedText.get(node)===current)return;
  const next=translateText(current);
  if(next!==current){
    translatedText.set(node,next);
    node.textContent=next;
  }else{
    translatedText.delete(node);
  }
}

function translateTree(root:ParentNode){
  if(root instanceof Element)translateAttributes(root);
  root.querySelectorAll?.('[placeholder],[aria-label],[alt],[title]').forEach(translateAttributes);
  const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
  let node=walker.nextNode();
  while(node){translateNode(node);node=walker.nextNode()}
}

export function initAutoTranslate(){
  if(typeof window==='undefined'||detectLanguage()==='it')return ()=>{};
  const run=()=>translateTree(document.body);
  window.requestAnimationFrame(run);
  const fast=window.setTimeout(run,200);
  const late=window.setTimeout(run,900);
  const observer=new MutationObserver(mutations=>{
    for(const mutation of mutations){
      if(mutation.type==='characterData'){translateNode(mutation.target);continue}
      if(mutation.type==='attributes'&&mutation.target instanceof Element){translateAttributes(mutation.target);continue}
      mutation.addedNodes.forEach(node=>{
        if(node.nodeType===Node.TEXT_NODE)translateNode(node);
        else if(node.nodeType===Node.ELEMENT_NODE)translateTree(node as Element);
      });
    }
  });
  observer.observe(document.body,{childList:true,subtree:true,characterData:true,attributes:true,attributeFilter:['placeholder','aria-label','alt','title']});
  return ()=>{observer.disconnect();window.clearTimeout(fast);window.clearTimeout(late)};
}
