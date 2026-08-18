import {EthersAdapter} from '@reown/appkit-adapter-ethers';
import {mainnet} from '@reown/appkit/networks';
import {createAppKit} from '@reown/appkit/react';

const configuredProjectId=(window.__HYPERCOPY_CONFIG__?.REOWN_PROJECT_ID??'').trim();
export const reownProjectId=configuredProjectId&&!configuredProjectId.includes('${')?configuredProjectId:'';
export const reownConfigured=reownProjectId.length>0;

if(reownConfigured){
 createAppKit({
  adapters:[new EthersAdapter()],
  networks:[mainnet],
  defaultNetwork:mainnet,
  projectId:reownProjectId,
  metadata:{
   name:'TRAXION',
   description:'TRAXION — Hyperliquid AI Trading Agent',
   url:window.location.origin,
   icons:[`${window.location.origin}/traxion-apple-touch-icon.png`]
  },
  themeMode:'dark',
  allWallets:'SHOW',
  enableWallets:true,
  enableInjected:true,
  enableEIP6963:true,
  enableCoinbase:true,
  enableBaseAccount:false,
  coinbasePreference:'eoaOnly',
  enableReconnect:false,
  enableNetworkSwitch:false,
  enableMobileFullScreen:true,
  enableWalletGuide:false,
  enableAuthLogger:false,
  enableEmbedded:false,
  allowUnsupportedChain:false,
  defaultAccountTypes:{eip155:'eoa'},
  features:{
   email:false,
   socials:false,
   analytics:false,
   swaps:false,
   onramp:false,
   send:false,
   receive:false,
   history:false,
   pay:false,
   smartSessions:false,
   reownAuthentication:false,
   allWallets:true,
   connectMethodsOrder:['wallet']
  }
 });
}
