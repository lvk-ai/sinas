// Tests for console/src/lib/configApply.ts: the config apply request body
// (prune plumbed through) and what a result says a prune removed.
//
// Run: npm test (node:test; the module is transpiled with esbuild).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const entry = fileURLToPath(new URL('../src/lib/configApply.ts', import.meta.url));
const { outputFiles } = await build({ entryPoints: [entry], bundle: true, format: 'esm', write: false, logLevel: 'silent' });
const m = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString('base64')}`);

test('the body always says whether to prune: off unless asked', () => {
  assert.deepEqual(m.configApplyBody('yaml'), { config: 'yaml', dryRun: false, force: false, prune: false });
  assert.deepEqual(m.configApplyBody('yaml', { dryRun: true, prune: true }), {
    config: 'yaml', dryRun: true, force: false, prune: true,
  });
  assert.deepEqual(m.configApplyBody('yaml', { force: true }), { config: 'yaml', dryRun: false, force: true, prune: false });
});

const change = (action, resourceType, resourceName) => ({ action, resourceType, resourceName });

test('removals are the delete changes, grouped by kind, kinds and names sorted', () => {
  const changes = [
    change('create', 'agents', 'ns/new'),
    change('delete', 'skills', 'ns/zeta'),
    change('delete', 'agents', 'ns/old'),
    change('unchanged', 'skills', 'ns/kept'),
    change('delete', 'skills', 'ns/alpha'),
  ];
  assert.deepEqual(m.removalsByKind(changes), [
    { kind: 'agents', names: ['ns/old'] },
    { kind: 'skills', names: ['ns/alpha', 'ns/zeta'] },
  ]);
  assert.equal(m.removalCount(changes), 3);
});

test('no changes means nothing removed', () => {
  assert.deepEqual(m.removalsByKind(undefined), []);
  assert.deepEqual(m.removalsByKind([change('update', 'agents', 'a')]), []);
  assert.equal(m.removalCount(null), 0);
});

test('what a prune kept or refused is picked out of the warnings and errors', () => {
  const warnings = [
    "Kept collection 'docs/files' although no longer declared: it holds 3 files. Delete it in the console to remove it with its contents.",
    "Agent 'a/b' is managed by another config; skipped",
  ];
  assert.deepEqual(m.keptByPrune(warnings), [warnings[0]]);
  const errors = [
    "Can't remove function 'ns/f': still used by schedule 'nightly'. Remove or change those too, or keep declaring it.",
    "Error removing skill 'ns/s': boom",
    'agents.0.name: field required',
  ];
  assert.deepEqual(m.blockedByPrune(errors), errors.slice(0, 2));
  assert.deepEqual(m.keptByPrune(undefined), []);
  assert.deepEqual(m.blockedByPrune(undefined), []);
});

test('resource counts read naturally', () => {
  assert.equal(m.pluralResources(1), '1 resource');
  assert.equal(m.pluralResources(0), '0 resources');
  assert.equal(m.pluralResources(4), '4 resources');
});
