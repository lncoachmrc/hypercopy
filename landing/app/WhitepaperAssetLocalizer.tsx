"use client";

import {useEffect} from "react";
import {detectLanguage} from "./i18n";
import {localizedWhitepaperAsset} from "./whitepaper-locale";

const PAGE_PATTERN=/trx-wp-(?:(?:en|es)-)?0([1-6])\.webp(?:$|[?#])/i;

function pageFromImage(image:HTMLImageElement){
  const source=image.getAttribute("src")||"";
  const match=source.match(PAGE_PATTERN);
  if(match)return Number(match[1]);
  const alt=image.getAttribute("alt")||"";
  const altMatch=alt.match(/\b([1-6])\b/);
  return altMatch?Number(altMatch[1]):null;
}

function localizeImage(image:HTMLImageElement){
  const page=pageFromImage(image);
  if(!page)return;
  const desired=localizedWhitepaperAsset(detectLanguage(),page);
  if(image.getAttribute("src")!==desired)image.setAttribute("src",desired);
}

function localizeViewer(){
  document.querySelectorAll<HTMLImageElement>(".whitepaper-viewer .viewer-page img").forEach(localizeImage);
}

export default function WhitepaperAssetLocalizer(){
  useEffect(()=>{
    localizeViewer();
    const observer=new MutationObserver(mutations=>{
      for(const mutation of mutations){
        if(mutation.type==="attributes"&&mutation.target instanceof HTMLImageElement){
          if(mutation.target.closest(".whitepaper-viewer"))localizeImage(mutation.target);
          continue;
        }
        mutation.addedNodes.forEach(node=>{
          if(!(node instanceof Element))return;
          if(node instanceof HTMLImageElement&&node.closest(".whitepaper-viewer"))localizeImage(node);
          node.querySelectorAll?.<HTMLImageElement>(".whitepaper-viewer .viewer-page img, .viewer-page img").forEach(localizeImage);
        });
      }
    });
    observer.observe(document.body,{childList:true,subtree:true,attributes:true,attributeFilter:["src"]});
    return()=>observer.disconnect();
  },[]);
  return null;
}
