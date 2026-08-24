import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

const walletLogin=await readFile(new URL('../src/WalletLogin.tsx',import.meta.url),'utf8');
const reown=await readFile(new URL('../src/reown.ts',import.meta.url),'utf8');

assert.doesNotMatch(walletLogin,/eth_chainId/,'login must not depend on the active EVM chain');
assert.doesNotMatch(walletLogin,/isEthereumMainnet/,'login must not restore a mainnet-only guard');
assert.doesNotMatch(walletLogin,/BrowserProvider\(walletProvider\s*,\s*1\)/,'signer provider must not pin Ethereum Mainnet');
assert.match(walletLogin,/BrowserProvider\(walletProvider\)/,'login must use the connected EIP-1193 provider without pinning a chain');
assert.match(walletLogin,/eth_accounts/,'login must still verify the connected account before and after signing');
assert.match(walletLogin,/sameAddress/,'login must still bind the signature flow to the selected address');
assert.match(reown,/allowUnsupportedChain:true/,'AppKit must allow a wallet session whose active EVM chain is not the preferred network');
assert.match(reown,/enableNetworkSwitch:false/,'TRAXION must not switch the user wallet network automatically during login');

console.log('wallet login chain-independence checks passed');
