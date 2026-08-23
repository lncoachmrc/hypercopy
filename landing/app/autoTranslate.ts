import {detectLanguage} from "./i18n";
import {LANDING_DICTIONARIES,type TranslationMap} from "./translations";
import {DATE_EN,DATE_ES} from "./date-translations";

const SKIP_TAGS=new Set(["SCRIPT","STYLE","NOSCRIPT","TEXTAREA","CODE","PRE"]);

function dictionary():TranslationMap{
  const language=detectLanguage();
  if(language==="it")return {};
  return {...LANDING_DICTIONARIES[language],...(language==="en"?DATE_EN:DATE_ES)};
}
function normalise(value:string){return value.replace(/\s+/g," ").trim()}
function escapeRegExp(value:string){return value.replace(/[.*+?^${}()|[\]\\]/g,"\\$&")}
function entries(){return Object.entries(dictionary()).sort((a,b)=>b[0].length-a[0].length)}

export function translateText(value:string){
  const all=entries();
  if(!all.length)return value;
  const compact=normalise(value);
  const exact=all.find(([source])=>normalise(source)===compact);
  if(exact){
    const prefix=value.match(/^\s*/)?.[0]||"";
    const suffix=value.match(/\s*$/)?.[0]||"";
    return `${prefix}${exact[1]}${suffix}`;
  }
  let output=value;
  for(const [source,target] of all)output=output.replace(new RegExp(escapeRegExp(source),"g"),target);
  return output;
}
function skipped(element:Element|null){return Boolean(element&&(SKIP_TAGS.has(element.tagName)||element.closest("[data-no-translate]")))}
function translateAttributes(element:Element){
  if(skipped(element))return;
  ["placeholder","aria-label","alt","title"].forEach(attribute=>{
    const value=element.getAttribute(attribute);
    if(!value)return;
    const next=translateText(value);
    if(next!==value)element.setAttribute(attribute,next);
  });
}
function translateNode(node:Node){
  if(node.nodeType!==Node.TEXT_NODE||!node.textContent?.trim()||skipped(node.parentElement))return;
  const next=translateText(node.textContent);
  if(next!==node.textContent)node.textContent=next;
}
function translateTree(root:ParentNode){
  if(root instanceof Element)translateAttributes(root);
  root.querySelectorAll?.("[placeholder],[aria-label],[alt],[title]").forEach(translateAttributes);
  const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
  let node=walker.nextNode();
  while(node){translateNode(node);node=walker.nextNode()}
}

export function initAutoTranslate(){
  if(typeof window==="undefined"||detectLanguage()==="it")return ()=>{};
  const run=()=>translateTree(document.body);
  window.requestAnimationFrame(run);
  const fast=window.setTimeout(run,200);
  const late=window.setTimeout(run,900);
  const observer=new MutationObserver(mutations=>{
    for(const mutation of mutations){
      if(mutation.type==="characterData"){translateNode(mutation.target);continue}
      if(mutation.type==="attributes"&&mutation.target instanceof Element){translateAttributes(mutation.target);continue}
      mutation.addedNodes.forEach(node=>{
        if(node.nodeType===Node.TEXT_NODE)translateNode(node);
        else if(node.nodeType===Node.ELEMENT_NODE)translateTree(node as Element);
      });
    }
  });
  observer.observe(document.body,{childList:true,subtree:true,characterData:true,attributes:true,attributeFilter:["placeholder","aria-label","alt","title"]});
  return ()=>{observer.disconnect();window.clearTimeout(fast);window.clearTimeout(late)};
}
