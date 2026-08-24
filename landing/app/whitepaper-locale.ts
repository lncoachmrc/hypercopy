import type {Language} from "./i18n";
import {traxionAsset} from "./config";

export function localizedWhitepaperAsset(language:Language,page:number){
  const n=String(page).padStart(2,"0");
  if(language==="en")return traxionAsset(`/whitepaper/en/trx-wp-en-${n}.webp`);
  if(language==="es")return traxionAsset(`/whitepaper/es/trx-wp-es-${n}.webp`);
  return traxionAsset(`/whitepaper/trx-wp-${n}.webp`);
}
