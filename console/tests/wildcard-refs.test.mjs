// Tests for console/src/lib/wildcardRefs.ts: wildcard entries in the agent
// editor's enabled_* lists.
//
// Run: npm test (node:test; the module is transpiled with esbuild).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const entry = fileURLToPath(new URL('../src/lib/wildcardRefs.ts', import.meta.url));
const { outputFiles } = await build({ entryPoints: [entry], bundle: true, format: 'esm', write: false, logLevel: 'silent' });
const m = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString('base64')}`);

test('only a whole name is a wildcard', () => {
  for (const ok of ['sales/*', '*/*']) {
    assert.equal(m.isWildcardRef(ok), true, ok);
    assert.equal(m.wildcardPatternProblem(ok), null, ok);
  }
  for (const bad of ['*/name', 'sales/send_*', 'sa*/*', '*', 'sales/name', '/*', '']) {
    assert.equal(m.isWildcardRef(bad), false, bad);
    assert.notEqual(m.wildcardPatternProblem(bad), null, bad);
  }
  assert.equal(m.wildcardPatternProblem('  sales/*  '), null);
});

test('wildcard entries of string and dict lists', () => {
  assert.deepEqual(m.wildcardEntries(['a/b', 'a/*', '*/*']), ['a/*', '*/*']);
  const stores = [{ store: 'crm/notes', access: 'readonly' }, { store: 'crm/*', access: 'readwrite' }];
  assert.deepEqual(m.wildcardEntries(stores, 'store'), [stores[1]]);
  assert.deepEqual(m.wildcardEntries(undefined, 'store'), []);
});

test('adding replaces the entry for the same reference; removing keeps the others', () => {
  const stores = [{ store: 'crm/*', access: 'readonly' }, { store: 'crm/notes', access: 'readwrite' }];
  assert.deepEqual(m.withEntry(stores, { store: 'crm/*', access: 'readwrite' }, 'store'), [
    { store: 'crm/notes', access: 'readwrite' },
    { store: 'crm/*', access: 'readwrite' },
  ]);
  assert.deepEqual(m.withoutRef(stores, 'crm/*', 'store'), [{ store: 'crm/notes', access: 'readwrite' }]);
  assert.deepEqual(m.withEntry(['ui/x'], 'ui/*'), ['ui/x', 'ui/*']);
});

test('a wildcard entry is edited in place', () => {
  const conns = [{ connector: 'apis/*', operations: [] }, { connector: 'apis/x', operations: ['get'] }];
  assert.deepEqual(m.withReplaced(conns, 'apis/*', { connector: 'apis/*', operations: ['list'] }, 'connector'), [
    { connector: 'apis/*', operations: ['list'] },
    { connector: 'apis/x', operations: ['get'] },
  ]);
});

test('settings typed as text', () => {
  assert.deepEqual(m.parseNameList(' get, list ,,'), ['get', 'list']);
  assert.deepEqual(m.parseNameList(''), []);
  assert.deepEqual(m.parseJsonObject(''), {});
  assert.deepEqual(m.parseJsonObject('{"post": {"channel": "x"}}'), { post: { channel: 'x' } });
  for (const bad of ['[]', '1', '{', 'null']) assert.equal(m.parseJsonObject(bad), null, bad);
});
