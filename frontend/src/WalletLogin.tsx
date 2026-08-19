import {useCallback,useEffect,useRef,useState} from 'react';
import {BrowserProvider,getAddress} from 'ethers';
import {type Provider,useAppKit,useAppKitAccount,useAppKitProvider,useAppKitState} from '@reown/appkit/react';
import {AuthFlowError,useAuth} from './auth';
import {reownConfigured} from './reown';

type Phase='idle'|'choosing'|'checking'|'signing'|'verifying';
class WalletFlowError extends Error{constructor(message:string){super(message);this.name='WalletFlowError';}}

function numericErrorCode(error:unknown):number|undefined{
 if(typeof error!=='object'||error===null)return undefined;
 if('code'in error){const code=(error as {code?:unknown}).code;if(typeof code==='number')return code;if(typeof code==='string'&&/^\d+$/.test(code))return Number(code);}
 if('cause'in error)return numericErrorCode((error as {cause?:unknown}).cause);
 if('info'in error){const info=(error as {info?:unknown}).info;if(typeof info==='object'&&info!==null&&'error'in info)return numericErrorCode((info as {error?:unknown}).error);}
 return undefined;
}
function sameAddress(a:string|undefined,b:string|undefined){if(!a||!b)return false;try{return getAddress(a)===getAddress(b);}catch{return false;}}
function isEthereumMainnet(chainId:unknown){
 if(typeof chainId==='number')return chainId===1;
 if(typeof chainId!=='string')return false;
 try{return BigInt(chainId)===1n;}catch{return false;}
}
function walletErrorMessage(error:unknown){
 if(error instanceof WalletFlowError||error instanceof AuthFlowError)return error.message;
 if(typeof navigator!=='undefined'&&!navigator.onLine)return 'Sei offline. Ripristina la connessione e riprova.';
 const code=numericErrorCode(error);
 if(code===4001)return 'Richiesta annullata. Puoi riprovare quando vuoi.';
 if(code===4900||code===4901)return 'Il wallet si è disconnesso. Collegalo di nuovo e riprova.';
 const message=error instanceof Error?error.message.toLowerCase():'';
 if(message.includes('relay')||message.includes('socket')||message.includes('walletconnect'))return 'Il servizio di collegamento wallet è temporaneamente non disponibile. Riprova.';
 if(message.includes('disconnected')||message.includes('disconnect'))return 'Il wallet si è disconnesso. Collegalo di nuovo e riprova.';
 return 'Impossibile completare il collegamento al wallet. Riprova.';
}

function MissingConfiguration(){return <><div className="alert error" role="alert">Configurazione wallet non disponibile. Imposta REOWN_PROJECT_ID nel servizio frontend.</div><button className="primary" disabled aria-disabled="true">Connetti wallet</button></>}

function ConfiguredWalletLogin(){
 const {open,close}=useAppKit();
 const {address,isConnected,status}=useAppKitAccount({namespace:'eip155'});
 const {walletProvider}=useAppKitProvider<Provider>('eip155');
 const modalState=useAppKitState();
 const {signIn,loading,error,clearError}=useAuth();
 const [phase,setPhase]=useState<Phase>('idle');
 const [localError,setLocalError]=useState<string|null>(null);
 const intentRef=useRef(false),runningRef=useRef(false),sawModalOpenRef=useRef(false);
 const latestAddress=useRef(address),latestConnected=useRef(isConnected);
 useEffect(()=>{latestAddress.current=address;latestConnected.current=isConnected;},[address,isConnected]);

 const assertStable=useCallback((expected:string)=>{
  if(!latestConnected.current||!sameAddress(latestAddress.current,expected))throw new WalletFlowError('L’indirizzo del wallet è cambiato. Riprova per generare una nuova richiesta.');
 },[]);
 const assertMainnet=useCallback(async(provider:Provider)=>{
  const chainId=await provider.request<unknown>({method:'eth_chainId'});
  if(!isEthereumMainnet(chainId))throw new WalletFlowError('Seleziona Ethereum Mainnet nel wallet e riprova. TRAXION non cambia rete automaticamente.');
 },[]);

 const authenticate=useCallback(async()=>{
  if(runningRef.current)return;
  if(!isConnected||!address||!walletProvider){setPhase('idle');return;}
  runningRef.current=true;intentRef.current=false;sawModalOpenRef.current=false;setLocalError(null);clearError();setPhase('checking');
  try{
   await close();
   const expected=getAddress(address);
   await assertMainnet(walletProvider);
   const accounts=await walletProvider.request<string[]>({method:'eth_accounts'});
   if(!accounts.some(account=>sameAddress(account,expected)))throw new WalletFlowError('Il wallet collegato non controlla l’indirizzo selezionato. Ricollegalo e riprova.');
   const provider=new BrowserProvider(walletProvider,1);
   const signer=await provider.getSigner(expected);
   if(!sameAddress(await signer.getAddress(),expected))throw new WalletFlowError('Il wallet collegato non controlla l’indirizzo selezionato. Ricollegalo e riprova.');

   await signIn(address,async message=>{
    assertStable(expected);
    await assertMainnet(walletProvider);
    const before=await walletProvider.request<string[]>({method:'eth_accounts'});
    if(!before.some(account=>sameAddress(account,expected)))throw new WalletFlowError('L’indirizzo del wallet è cambiato. Riprova per generare una nuova richiesta.');
    setPhase('signing');
    let signature:string;
    try{signature=await signer.signMessage(message);}catch(e){throw new WalletFlowError(walletErrorMessage(e));}
    assertStable(expected);
    await assertMainnet(walletProvider);
    const after=await walletProvider.request<string[]>({method:'eth_accounts'});
    if(!after.some(account=>sameAddress(account,expected))||!sameAddress(await signer.getAddress(),expected))throw new WalletFlowError('L’indirizzo del wallet è cambiato. Riprova per generare una nuova richiesta.');
    setPhase('verifying');
    return signature;
   });
  }catch(e){setLocalError(walletErrorMessage(e));}finally{runningRef.current=false;setPhase('idle');}
 },[address,assertMainnet,assertStable,clearError,close,isConnected,signIn,walletProvider]);

 useEffect(()=>{
  if(!intentRef.current||runningRef.current)return;
  if(modalState.open)sawModalOpenRef.current=true;
  if(isConnected&&address&&walletProvider){void authenticate();return;}
  if(sawModalOpenRef.current&&!modalState.open&&!isConnected){intentRef.current=false;sawModalOpenRef.current=false;setPhase('idle');setLocalError('Richiesta annullata. Puoi riprovare quando vuoi.');}
 },[address,authenticate,isConnected,modalState.open,walletProvider]);

 const start=useCallback(async()=>{
  if(runningRef.current||phase!=='idle')return;
  clearError();setLocalError(null);
  if(typeof navigator!=='undefined'&&!navigator.onLine){setLocalError('Sei offline. Ripristina la connessione e riprova.');return;}
  intentRef.current=true;sawModalOpenRef.current=false;
  if(isConnected&&address&&walletProvider){void authenticate();return;}
  setPhase('choosing');
  try{await open({view:'Connect',namespace:'eip155'});}catch(e){intentRef.current=false;setPhase('idle');setLocalError(walletErrorMessage(e));}
 },[address,authenticate,clearError,isConnected,open,phase,walletProvider]);

 const busy=loading||phase!=='idle';
 const statusText=phase==='choosing'||status==='connecting'?'Scegli il wallet da collegare.':phase==='checking'?'Wallet collegato. Preparazione della firma…':phase==='signing'?'Wallet collegato. Conferma la firma nell’app.':phase==='verifying'?'Firma ricevuta. Verifica accesso…':'La firma conferma il controllo del wallet e non invia transazioni.';
 const buttonText=phase==='choosing'?'Scegli il wallet…':phase==='checking'?'Preparazione…':phase==='signing'?'Conferma nel wallet…':phase==='verifying'?'Verifica…':'Connetti wallet';
 return <>{(localError||error)&&<div className="alert error" role="alert">{localError||error}</div>}<p className="muted" aria-live="polite" aria-busy={busy}>{statusText}</p><button className="primary" disabled={busy} aria-disabled={busy} aria-busy={busy} onClick={()=>void start()}>{buttonText}</button></>;
}

export default function WalletLogin(){return reownConfigured?<ConfiguredWalletLogin/>:<MissingConfiguration/>;}
