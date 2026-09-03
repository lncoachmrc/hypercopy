import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const nginxPath = resolve(root, 'nginx.conf');
const config = readFileSync(nginxPath, 'utf8');
const expected = 'add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;';

function fail(message) {
  console.error(`F-05 HSTS check failed: ${message}`);
  process.exit(1);
}

const firstLocation = config.indexOf('location ');
const serverPreamble = firstLocation >= 0 ? config.slice(0, firstLocation) : config;
if (!serverPreamble.includes(expected)) {
  fail('missing Strict-Transport-Security at server scope');
}

const locationStart = /\blocation\b[^\{]*\{/g;
let match;
while ((match = locationStart.exec(config)) !== null) {
  let depth = 1;
  let cursor = match.index + match[0].length;
  while (cursor < config.length && depth > 0) {
    if (config[cursor] === '{') depth += 1;
    if (config[cursor] === '}') depth -= 1;
    cursor += 1;
  }
  const block = config.slice(match.index, cursor);
  if (block.includes('add_header ') && !block.includes(expected)) {
    const label = match[0].replace(/\s+/g, ' ').trim();
    fail(`${label} defines add_header but does not repeat HSTS, so nginx would drop inherited HSTS`);
  }
}

console.log('F-05 HSTS configuration check passed');
