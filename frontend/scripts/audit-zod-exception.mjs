/** One authorized, expiring exception. This is NOT a general audit allowlist. */
import {createHash} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {dirname, resolve} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';

const ADVISORY = 'GHSA-mrhx-c7xc-m3w5';
const ADVISORY_URL = `https://github.com/advisories/${ADVISORY}`;
const ISSUED = '2026-11-09T00:00:00.000Z';
// 9 December 2026, 00:00 Europe/Rome. No environment or CLI override.
const EXPIRES = '2026-12-08T23:00:00.000Z';
const REVIEWED_BLOBS = Object.freeze({
  'package.json': 'cbd042be13df238c76988b47abde0ed780b8e653',
  'package-lock.json': '61db0c5d4fa1a085fa24098d1d3cbd98acc53ef6',
  'src/reown.ts': 'f7c80f026fd9176f70517eca5bf8e179e843bd36',
});
// Exact affected paths/versions from PR #300, not arbitrary descendants of Zod.
const REVIEWED_NODES = Object.freeze({
  'node_modules/zod': '3.25.76',
  'node_modules/@reown/appkit-wallet/node_modules/zod': '3.22.4',
  'node_modules/@reown/appkit-wallet': '1.8.24',
  'node_modules/@reown/appkit': '1.8.24',
  'node_modules/@reown/appkit-adapter-ethers': '1.8.24',
  'node_modules/@reown/appkit-controllers': '1.8.24',
  'node_modules/@reown/appkit-pay': '1.8.24',
  'node_modules/@reown/appkit-scaffold-ui': '1.8.24',
  'node_modules/@reown/appkit-ui': '1.8.24',
  'node_modules/@reown/appkit-utils': '1.8.24',
  'node_modules/@coinbase/cdp-sdk': '1.61.0',
  'node_modules/abitype': '1.0.6',
});
const SEVERITIES = ['info', 'low', 'moderate', 'high', 'critical'];
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const integer = value => Number.isSafeInteger(value) && value >= 0;
function requireThat(condition, message) {
  if (!condition) throw new Error(message);
}

export function checkClock(now = new Date()) {
  const ms = now.getTime();
  requireThat(Number.isFinite(ms) && ms >= Date.parse(ISSUED) && ms < Date.parse(EXPIRES),
    `outside approved exception window (${ISSUED} <= UTC < ${EXPIRES}); review/removal required`);
}

export function checkBlob(name, text, expected) {
  // Match the reviewed Git blob byte-for-byte, including dependency edges and
  // integrity hashes. This is a drift check, not a package-signature substitute.
  const bytes = Buffer.from(text, 'utf8');
  const actual = createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex');
  requireThat(actual === expected, `reviewed source changed: ${name}; exception review required`);
}

export function checkReownConfig(source) {
  checkBlob('src/reown.ts', source, REVIEWED_BLOBS['src/reown.ts']);
  // The whole-file binding above prevents comments, spreads or duplicate
  // properties from disguising a changed effective configuration.
  for (const name of ['email', 'socials', 'enableEmbedded', 'enableBaseAccount']) {
    const matches = source.match(new RegExp(`\\b${name}\\s*:\\s*false\\b`, 'g'));
    requireThat(matches?.length === 1, `reviewed source mitigation missing: ${name}=false`);
  }
}

export function checkProcess(result) {
  requireThat(!result.error && !result.signal, 'audit process failure/timeout/signal');
  requireThat(result.status === 0 || result.status === 1, 'unexpected audit exit status');
}

export function evaluateAudit(raw, status, lock) {
  checkProcess({status});
  let report;
  try { report = JSON.parse(raw); } catch { throw new Error('invalid/truncated audit JSON'); }
  requireThat(object(report), 'invalid audit schema');
  requireThat(!Object.hasOwn(report, 'error'), 'npm audit error response');
  requireThat(report.auditReportVersion === 2 && object(report.vulnerabilities)
    && object(report.metadata) && object(report.metadata.vulnerabilities)
    && object(report.metadata.dependencies) && integer(report.metadata.dependencies.total),
  'unsupported/incomplete audit schema');
  requireThat(object(lock) && lock.lockfileVersion === 3 && object(lock.packages), 'invalid lock schema');
  const entries = Object.entries(report.vulnerabilities);
  const counts = report.metadata.vulnerabilities;
  requireThat([...SEVERITIES, 'total'].every(key => integer(counts[key])), 'invalid count schema');
  requireThat(counts.total === entries.length
    && SEVERITIES.reduce((sum, key) => sum + counts[key], 0) === counts.total, 'audit count mismatch');
  requireThat(status === (entries.length ? 1 : 0), 'audit exit/report mismatch');
  const histogram = Object.fromEntries(SEVERITIES.map(key => [key, 0]));
  const roots = new Set();
  const links = new Map();
  for (const [name, item] of entries) {
    requireThat(Object.hasOwn(REVIEWED_NODES, `node_modules/${name}`), `unapproved package: ${name}`);
    requireThat(object(item) && item.name === name && SEVERITIES.includes(item.severity)
      && Array.isArray(item.nodes) && item.nodes.length > 0
      && Array.isArray(item.via) && Array.isArray(item.effects), `invalid finding schema: ${name}`);
    histogram[item.severity]++;
    requireThat(item.via.length > 0, `empty via: ${name}`);
    requireThat(new Set(item.nodes).size === item.nodes.length, `duplicate node: ${name}`);
    for (const node of item.nodes) {
      requireThat(typeof node === 'string' && Object.hasOwn(REVIEWED_NODES, node)
        && (node === `node_modules/${name}` || (name === 'zod' && node === 'node_modules/@reown/appkit-wallet/node_modules/zod'))
        && lock.packages[node]?.version === REVIEWED_NODES[node], `unapproved node/version: ${name}`);
    }
    const dependencies = [];
    for (const via of item.via) {
      if (typeof via === 'string') {
        requireThat(Object.hasOwn(report.vulnerabilities, via), `unresolved dependency: ${via}`);
        dependencies.push(via);
      } else {
        requireThat(name === 'zod' && object(via) && via.name === 'zod' && via.dependency === 'zod'
          && via.url === ADVISORY_URL && via.severity === 'high' && via.range === '<4.6.6'
          && integer(via.source) && via.source > 0 && typeof via.title === 'string' && via.title.length > 0,
        `unapproved advisory in ${name}`);
        roots.add(name);
      }
    }
    for (const effect of item.effects) {
      requireThat(typeof effect === 'string' && Object.hasOwn(report.vulnerabilities, effect), 'unresolved dependency effect');
    }
    links.set(name, dependencies);
  }
  requireThat(SEVERITIES.every(key => histogram[key] === counts[key]), 'audit severity count mismatch');
  // Reown's dependency graph contains legitimate cycles. Every finding must
  // nevertheless have a path to an approved leaf; isolated cycles are rejected.
  const rooted = new Set(roots);
  let previousSize;
  do {
    previousSize = rooted.size;
    for (const [name, dependencies] of links) {
      if (dependencies.some(dep => rooted.has(dep))) rooted.add(name);
    }
  } while (rooted.size !== previousSize);
  requireThat(entries.every(([name]) => rooted.has(name)), 'unrooted finding/cycle');
  return {rawFindings: entries.length, exceptionApplied: entries.length > 0};
}

export function main() {
  const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..');
  const evidence = resolve(frontend, 'audit-evidence');
  mkdirSync(evidence, {recursive: true, mode: 0o700});
  const save = (name, value) => writeFileSync(resolve(evidence, name), value, {mode: 0o600});
  const command = ['audit', '--json', '--audit-level=low', '--include=dev', '--include=optional', '--include=peer', '--registry=https://registry.npmjs.org'];
  const result = spawnSync('npm', command, {cwd: frontend, encoding: 'utf8', timeout: 120000, maxBuffer: 8 * 1024 * 1024});
  // Persist the original report before any decision. Do not remove or rewrite
  // findings. Transport/schema failures never count as a clean audit.
  save('npm-audit.raw.json', result.stdout ?? '');
  save('npm-audit.stderr.txt', result.stderr ?? '');
  save('npm-audit.process.json', JSON.stringify({command: ['npm', ...command], status: result.status, signal: result.signal, error: result.error?.code ?? null}, null, 2) + '\n');
  const decision = {checkedAt: new Date().toISOString(), advisory: ADVISORY, expiresAt: EXPIRES, rawExitCode: result.status, decision: 'FAIL'};
  try {
    requireThat(process.argv.length === 2, 'unexpected CLI arguments; no override is supported');
    checkProcess(result);
    checkClock();
    const lockText = readFileSync(resolve(frontend, 'package-lock.json'), 'utf8');
    checkBlob('package-lock.json', lockText, REVIEWED_BLOBS['package-lock.json']);
    checkBlob('package.json', readFileSync(resolve(frontend, 'package.json'), 'utf8'), REVIEWED_BLOBS['package.json']);
    checkReownConfig(readFileSync(resolve(frontend, 'src/reown.ts'), 'utf8'));
    const outcome = evaluateAudit(result.stdout, result.status, JSON.parse(lockText));
    Object.assign(decision, outcome, {decision: outcome.exceptionApplied ? 'PASS_WITH_APPROVED_EXCEPTION' : 'PASS_CLEAN'});
    console.log(`${decision.decision}: raw npm exit=${result.status}; raw findings=${outcome.rawFindings}; exception=${ADVISORY}; expires=${EXPIRES}`);
  } catch (error) {
    decision.reason = error instanceof Error ? error.message : 'unknown audit gate failure';
    console.error(`AUDIT GATE FAILED: ${decision.reason}`);
    process.exitCode = 1;
  } finally {
    save('gate-decision.json', JSON.stringify(decision, null, 2) + '\n');
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) main();
