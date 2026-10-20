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

test('wildcard entries of string and dict lists, by position', () => {
  assert.deepEqual(m.wildcardIndices(['a/b', 'a/*', '*/*']), [1, 2]);
  const stores = [{ store: 'crm/notes', access: 'readonly' }, { store: 'crm/*', access: 'readwrite' }];
  assert.deepEqual(m.wildcardIndices(stores, 'store'), [1]);
  assert.deepEqual(m.wildcardIndices(undefined, 'store'), []);
  assert.equal(m.hasRef(stores, 'crm/*', 'store'), true);
  assert.equal(m.hasRef(stores, 'hr/*', 'store'), false);
});

test('editing or removing one entry leaves a sibling for the same pattern alone', () => {
  const conns = [
    { connector: 'apis/*', operations: ['get'] },
    { connector: 'apis/*', operations: ['list'], parameters: { list: { x: 1 } } },
    { connector: 'apis/x', operations: [] },
  ];
  assert.deepEqual(m.withReplacedAt(conns, 0, { connector: 'apis/*', operations: ['get', 'put'] }), [
    { connector: 'apis/*', operations: ['get', 'put'] },
    conns[1],
    conns[2],
  ]);
  assert.deepEqual(m.withoutAt(conns, 0), [conns[1], conns[2]]);
});

test('settings typed as text', () => {
  assert.deepEqual(m.parseNameList(' get, list ,,'), ['get', 'list']);
  assert.deepEqual(m.parseNameList(''), []);
  assert.deepEqual(m.parseJsonObject(''), {});
  assert.deepEqual(m.parseJsonObject('{"post": {"channel": "x"}}'), { post: { channel: 'x' } });
  for (const bad of ['[]', '1', '{', 'null']) assert.equal(m.parseJsonObject(bad), null, bad);
});
