import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

const walletLogin=await readFile(new URL('../src/WalletLogin.tsx',import.meta.url),'utf8');
const reown=await readFile(new URL('../src/reown.ts',import.meta.url),'utf8');

assert.doesNotMatch(walletLogin,/eth_chainId/,'login must not depend on the active EVM chain');
assert.doesNotMatch(walletLogin,/isEthereumMainnet/,'login must not restore a mainnet-only guard');
assert.doesNotMatch(walletLogin,/BrowserProvider\(walletProvider\s*,\s*1\)/,'signer provider must not pin Ethereum Mainnet');
assert.match(walletLogin,/BrowserProvider\(walletProvider\)/,'login must use the connected EIP-1193 provider without pinning a chain');
assert.match(reown,/allowUnsupportedChain:true/,'AppKit must allow a wallet session whose active EVM chain is not the preferred network');
assert.match(reown,/enableNetworkSwitch:false/,'TRAXION must not switch the user wallet network automatically during login');

const callbackStart=walletLogin.indexOf("await signIn(address,async message=>{");
const callbackEnd=walletLogin.indexOf("return signature;\n   });",callbackStart);
assert.ok(callbackStart>=0&&callbackEnd>callbackStart,'signIn signature callback must remain present');
const signatureFlow=walletLogin.slice(callbackStart,callbackEnd);
const signIndex=signatureFlow.indexOf('signer.signMessage(message)');
assert.ok(signIndex>0,'signature callback must sign the challenge message');

const beforeSign=signatureFlow.slice(0,signIndex);
const afterSign=signatureFlow.slice(signIndex+'signer.signMessage(message)'.length);

assert.match(beforeSign,/assertStable\(expected\)/,'address stability must be checked immediately before signing');
assert.match(beforeSign,/const before=await walletProvider\.request<string\[]>\(\{method:'eth_accounts'\}\)/,'connected accounts must be read immediately before signing');
assert.match(beforeSign,/before\.some\(account=>sameAddress\(account,expected\)\)/,'selected address must still belong to the connected wallet before signing');

assert.match(afterSign,/assertStable\(expected\)/,'address stability must be checked immediately after signing');
assert.match(afterSign,/const after=await walletProvider\.request<string\[]>\(\{method:'eth_accounts'\}\)/,'connected accounts must be read immediately after signing');
assert.match(afterSign,/after\.some\(account=>sameAddress\(account,expected\)\)/,'selected address must still belong to the connected wallet after signing');
assert.match(afterSign,/sameAddress\(await signer\.getAddress\(\),expected\)/,'signer address must still match the selected address after signing');

console.log('wallet login chain-independence checks passed');
