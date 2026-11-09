import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import {checkBlob, checkClock, checkReownConfig, checkProcess, evaluateAudit} from './audit-zod-exception.mjs';

const ADVISORY_URL = 'https://github.com/advisories/GHSA-mrhx-c7xc-m3w5';
const config = readFileSync(new URL('../src/reown.ts', import.meta.url), 'utf8');
const versions = {
  zod: '3.25.76', '@reown/appkit-wallet': '1.8.24',
  '@reown/appkit': '1.8.24', '@reown/appkit-adapter-ethers': '1.8.24',
  '@reown/appkit-controllers': '1.8.24', '@reown/appkit-pay': '1.8.24',
  '@reown/appkit-scaffold-ui': '1.8.24', '@reown/appkit-ui': '1.8.24',
  '@reown/appkit-utils': '1.8.24', '@coinbase/cdp-sdk': '1.61.0', abitype: '1.0.6',
};
const leaf = {source: 1, name: 'zod', dependency: 'zod', title: 'Audit fixture', url: ADVISORY_URL, severity: 'high', range: '<4.6.6'};
function fixture() {
  const packages = {};
  const vulnerabilities = {};
  for (const [name, version] of Object.entries(versions)) {
    const path = `node_modules/${name}`;
    packages[path] = {version};
    vulnerabilities[name] = {name, severity: 'high', isDirect: false, via: name === 'zod' ? [structuredClone(leaf)] : ['zod'], effects: [], range: '*', nodes: [path], fixAvailable: false};
  }
  const nested = 'node_modules/@reown/appkit-wallet/node_modules/zod';
  packages[nested] = {version: '3.22.4'};
  vulnerabilities.zod.nodes.push(nested);
  return {report: {auditReportVersion: 2, vulnerabilities, metadata: {vulnerabilities: {info: 0, low: 0, moderate: 0, high: 11, critical: 0, total: 11}, dependencies: {total: 442}}}, lock: {lockfileVersion: 3, packages}};
}
function evaluate(f = fixture(), status = 1) {
  return evaluateAudit(JSON.stringify(f.report), status, f.lock);
}
function reject(change, pattern) {
  const f = fixture(); change(f);
  assert.throws(() => evaluate(f), pattern);
}

test('accepts only the reviewed Zod advisory, retaining the raw finding count', () => {
  assert.deepEqual(evaluate(), {rawFindings: 11, exceptionApplied: true});
});
test('handles legitimate Reown dependency cycles connected to the approved leaf', () => {
  const f = fixture();
  f.report.vulnerabilities['@reown/appkit'].via = ['@reown/appkit-wallet', '@reown/appkit-controllers'];
  f.report.vulnerabilities['@reown/appkit-controllers'].via = ['@reown/appkit'];
  assert.equal(evaluate(f).rawFindings, 11);
});
test('passes a genuinely clean complete report without applying an exception', () => {
  const f = fixture(); f.report.vulnerabilities = {};
  f.report.metadata.vulnerabilities = {info: 0, low: 0, moderate: 0, high: 0, critical: 0, total: 0};
  assert.deepEqual(evaluate(f, 0), {rawFindings: 0, exceptionApplied: false});
});
for (const severity of ['info', 'low', 'moderate', 'high', 'critical']) {
  test(`blocks another advisory of severity ${severity}, including mixed Zod causes`, () => {
    reject(f => f.report.vulnerabilities.zod.via.push({...leaf, severity, url: 'https://github.com/advisories/GHSA-other-other-0000'}), /unapproved advisory/);
  });
}
for (const [field, value] of [['url', `${ADVISORY_URL}?other=1`], ['name', 'another-package'], ['dependency', 'another-package'], ['severity', 'critical'], ['range', '<99.0.0']]) {
  test(`rejects changed advisory ${field}`, () => reject(f => f.report.vulnerabilities.zod.via[0][field] = value, /unapproved advisory/));
}
test('rejects an allowed ID reported against a different package', () => {
  reject(f => f.report.vulnerabilities.abitype.via = [structuredClone(leaf)], /unapproved advisory/);
});
test('rejects an uncensused dependency location', () => {
  reject(f => f.report.vulnerabilities.zod.nodes.push('node_modules/other/node_modules/zod'), /unapproved node/);
});
test('rejects a changed locked version', () => reject(f => f.lock.packages['node_modules/zod'].version = '3.99.0', /unapproved node/));
test('rejects a finding absent from the lockfile', () => reject(f => delete f.lock.packages['node_modules/zod'], /unapproved node/));
test('rejects an unknown affected package even with the approved root', () => {
  reject(f => {f.report.vulnerabilities.unknown = {...f.report.vulnerabilities.abitype, name: 'unknown'}; f.report.metadata.vulnerabilities.high++; f.report.metadata.vulnerabilities.total++;}, /unapproved package/);
});
test('rejects dangling transitive references', () => reject(f => f.report.vulnerabilities.abitype.via = ['missing'], /unresolved dependency/));
test('rejects isolated cycles without the approved advisory', () => {
  reject(f => {f.report.vulnerabilities.abitype.via = ['@coinbase/cdp-sdk']; f.report.vulnerabilities['@coinbase/cdp-sdk'].via = ['abitype'];}, /unrooted finding/);
});
test('rejects empty causes', () => reject(f => f.report.vulnerabilities.abitype.via = [], /empty via/));
test('rejects duplicate nodes', () => reject(f => f.report.vulnerabilities.abitype.nodes.push('node_modules/abitype'), /duplicate node/));
test('rejects metadata and findings count mismatch', () => reject(f => f.report.metadata.vulnerabilities.total = 0, /count mismatch/));
test('rejects severity histogram mismatch', () => reject(f => {f.report.metadata.vulnerabilities.high = 10; f.report.metadata.vulnerabilities.low = 1;}, /count mismatch/));
test('rejects audit service errors embedded in JSON', () => reject(f => f.report.error = {code: 'E503'}, /audit error/));
for (const raw of ['', 'not JSON', '{"auditReportVersion":2', '{}', '[]', 'null']) {
  test(`rejects incomplete/malformed report ${JSON.stringify(raw)}`, () => assert.throws(() => evaluateAudit(raw, 0, fixture().lock), /JSON|schema/));
}
test('rejects unknown audit report version', () => reject(f => f.report.auditReportVersion = 3, /schema/));
test('rejects missing dependency metadata', () => reject(f => delete f.report.metadata.dependencies, /schema/));
test('rejects inconsistent successful exit with findings', () => assert.throws(() => evaluate(fixture(), 0), /exit/));
test('rejects nonzero exit with an empty report', () => {
  const f = fixture(); f.report.vulnerabilities = {}; f.report.metadata.vulnerabilities = {info: 0, low: 0, moderate: 0, high: 0, critical: 0, total: 0};
  assert.throws(() => evaluate(f, 1), /exit/);
});
for (const status of [2, 127, null]) test(`rejects abnormal exit ${status}`, () => assert.throws(() => evaluate(fixture(), status), /exit/));
test('transport timeout cannot be mistaken for accepted findings', () => assert.throws(() => checkProcess({status: 1, error: new Error('ETIMEDOUT')}), /process/));
test('signal cannot be mistaken for accepted findings', () => assert.throws(() => checkProcess({status: 1, signal: 'SIGTERM'}), /process/));
test('normal npm statuses 0 and 1 reach report validation', () => {checkProcess({status: 0}); checkProcess({status: 1});});
test('accepts the approved window until the exact expiry boundary', () => {
  checkClock(new Date('2026-11-09T00:00:00Z')); checkClock(new Date('2026-12-08T22:59:59.999Z'));
});
for (const value of ['2026-12-08T23:00:00Z', '2026-12-09T00:00:00Z', '2027-01-01T00:00:00Z', '2026-11-08T23:59:59Z', 'invalid']) {
  test(`fails closed for clock ${value}`, () => assert.throws(() => checkClock(new Date(value)), /exception window/));
}
test('recognizes the exact reviewed Reown source', () => checkReownConfig(config));
for (const option of ['email', 'socials', 'enableEmbedded', 'enableBaseAccount']) {
  test(`fails when ${option} is enabled`, () => assert.throws(() => checkReownConfig(config.replace(`${option}:false`, `${option}:true`)), /reviewed source/));
  test(`fails when ${option} is removed`, () => assert.throws(() => checkReownConfig(config.replace(`${option}:false,`, '')), /reviewed source/));
}
test('a commented false option cannot hide an enabled option', () => {
  assert.throws(() => checkReownConfig(config.replace('enableEmbedded:false', 'enableEmbedded:true // enableEmbedded:false')), /reviewed source/);
});
test('even a source-only refactor requires a new exception review', () => assert.throws(() => checkReownConfig(`${config}\n`), /reviewed source/));
test('git-blob binding detects content drift', () => {
  checkBlob('fixture', 'hello\n', 'ce013625030ba8dba906f756967f9e9ca394464a');
  assert.throws(() => checkBlob('fixture', 'hello!\n', 'ce013625030ba8dba906f756967f9e9ca394464a'), /reviewed source/);
});
