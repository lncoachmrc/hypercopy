import {detectLanguage} from './i18n';

export function translateDialogText(value:string){
  const language=detectLanguage();
  if(language==='it')return value;

  if(language==='en'){
    return value
      .replace('Attivare AI ON? La Capital Intelligence potrà solo ridurre in modo conservativo il target di capitale. Non può creare ordini né superare il Risk Engine.','Enable AI ON? Capital Intelligence may only conservatively reduce the capital target. It cannot create orders or override the Risk Engine.')
      .replace('Attivare il trading automatizzato della strategia ibrida sulla rete ','Enable automated hybrid-strategy trading on the ')
      .replace('? Verranno utilizzati fondi reali.','? Real funds will be used.')
      .replace('Chiudere tutte le posizioni TRAXION gestite? La strategia verrà messa automaticamente in PAUSA per evitare riaperture.','Close all TRAXION-managed positions? The strategy will automatically be set to PAUSED to prevent reopening.')
      .replace('Sincronizzare SOLO la leva BTC sull’ACCOUNT TESTNET? L’account deve essere PAUSED e non verranno aperti/chiusi ordini.','Synchronize ONLY BTC leverage on the TESTNET ACCOUNT? The account must be PAUSED and no orders will be opened or closed.')
      .replace(/^Attivare la strategia per (.+) su (.+)\? La riconciliazione creerà immediatamente gli ordini consentiti dal Risk Engine\.$/,'Activate the strategy for $1 on $2? Reconciliation will immediately create the orders allowed by the Risk Engine.')
      .replace('Emergency stop?','Emergency stop?');
  }

  return value
    .replace('Attivare AI ON? La Capital Intelligence potrà solo ridurre in modo conservativo il target di capitale. Non può creare ordini né superare il Risk Engine.','¿Activar AI ON? Capital Intelligence solo podrá reducir de forma conservadora el objetivo de capital. No puede crear órdenes ni superar el Risk Engine.')
    .replace('Attivare il trading automatizzato della strategia ibrida sulla rete ','¿Activar el trading automatizado de la estrategia híbrida en la red ')
    .replace('? Verranno utilizzati fondi reali.','? Se utilizarán fondos reales.')
    .replace('Chiudere tutte le posizioni TRAXION gestite? La strategia verrà messa automaticamente in PAUSA per evitare riaperture.','¿Cerrar todas las posiciones gestionadas por TRAXION? La estrategia se pondrá automáticamente en PAUSA para evitar reaperturas.')
    .replace('Sincronizzare SOLO la leva BTC sull’ACCOUNT TESTNET? L’account deve essere PAUSED e non verranno aperti/chiusi ordini.','¿Sincronizar SOLO el apalancamiento BTC en la CUENTA TESTNET? La cuenta debe estar en PAUSA y no se abrirán ni cerrarán órdenes.')
    .replace(/^Attivare la strategia per (.+) su (.+)\? La riconciliazione creerà immediatamente gli ordini consentiti dal Risk Engine\.$/,'¿Activar la estrategia para $1 en $2? La reconciliación creará inmediatamente las órdenes permitidas por el Risk Engine.')
    .replace('Emergency stop?','¿Parada de emergencia?');
}
