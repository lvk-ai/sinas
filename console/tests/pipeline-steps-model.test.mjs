// Tests for console/src/components/pipeline-steps/model.ts — the step input
// conversions (fields ⇄ JMESPath expression ⇄ literal text) must never change
// what a step receives. Expected expressions here were checked against the
// backend's resolver (app/services/pipeline_mapping.py, python jmespath):
// each expression and its fields form resolve to the same value.
//
// Run: npm test (node:test; model.ts is transpiled with esbuild, which vite
// already ships, so no extra dependency).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const entry = fileURLToPath(new URL('../src/components/pipeline-steps/model.ts', import.meta.url));
const { outputFiles } = await build({ entryPoints: [entry], bundle: true, format: 'esm', write: false, logLevel: 'silent' });
const m = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString('base64')}`);

const INFO_FLOW = '{version: steps.fetch.output.body.version, auth_mode: steps.fetch.output.body.auth_mode, fail: input.fail}';

test('expression -> fields -> expression round-trips a multiselect hash exactly', () => {
  const input = m.expressionToInput(INFO_FLOW);
  assert.deepEqual(input, {
    'version.$': 'steps.fetch.output.body.version',
    'auth_mode.$': 'steps.fetch.output.body.auth_mode',
    'fail.$': 'input.fail',
  });
  assert.equal(m.inputToExpression(input), INFO_FLOW);
});

test('hash values: literals become literal fields, everything else stays an expression', () => {
  const expr = "{a: `1`, b: 'hi', c: `{\"x\": [1,2]}`, \"d-e\": foo[?a == 'x,y'].b, f: `\"str\"`, g: a || b, h: {n: x, m: y}}";
  const input = m.expressionToInput(expr);
  assert.deepEqual(input, {
    a: 1,
    b: 'hi',
    c: { x: [1, 2] },
    'd-e.$': "foo[?a == 'x,y'].b",
    f: 'str',
    'g.$': 'a || b',
    'h.$': '{n: x, m: y}',
  });
  assert.deepEqual(m.expressionToInput(m.inputToExpression(input)), input);
});

test('a JMESPath literal holding `.$` keys stays an expression (a fields literal would be evaluated)', () => {
  assert.deepEqual(m.expressionToInput('{a: `{"k.$": "x"}`}'), { 'a.$': '`{"k.$": "x"}`' });
});

test('expressions that are not a top-level hash are refused, not mangled', () => {
  for (const expr of [
    'steps.fetch.output.body',
    '{a: x}.a',
    '{a: x} | b',
    '[{version: steps.enrich.output.version}]',
    '`[1, 2]`',
    "'text'",
    '{a: }',
    '{a: x',
    '{"x.$": \'hi\'}', // as a fields key, `x.$` would become an expression key
    '{a: x, a: y}',
  ]) {
    assert.equal(m.expressionToInput(expr), null, expr);
  }
});

test('a literal-only object converts to a hash that splits back into fields', () => {
  const input = { a: 1, b: 'x', 'c d': [1, 'y'], e: { k: null } };
  const expr = m.inputToExpression(input);
  assert.equal(expr, "{a: `1`, b: 'x', \"c d\": `[1,\"y\"]`, e: `{\"k\":null}`}");
  assert.deepEqual(m.expressionToInput(expr), input);
});

test('an empty input is the literal `{}` (the backend rejects an empty expression)', () => {
  assert.equal(m.inputToExpression({}), '`{}`');
  assert.equal(m.inputToExpression(undefined), '`{}`');
  assert.deepEqual(m.expressionToInput('`{}`'), {});
  assert.deepEqual(m.expressionToInput(''), {});
  assert.deepEqual(m.expressionToInput('`{"a": 1}`'), { a: 1 });
});

test('nested templates convert to nested hashes', () => {
  const input = { nested: { 'k.$': 'input.fail', lit: [1, 'x'] }, 'e.$': 'run.id', q: "it's" };
  assert.equal(m.inputToExpression(input), "{nested: {k: input.fail, lit: `[1,\"x\"]`}, e: run.id, q: `\"it's\"`}");
});

test('literal text round-trips every value type', () => {
  for (const value of ['hello', '42', 'true', 'null', '=x', '', ' 42', '{"a":1}', 42, 1.5, true, false, null, { a: 1 }, [1, 'x']]) {
    assert.deepEqual(m.parseLiteral(m.literalText(value)), value, JSON.stringify(value));
  }
  assert.equal(m.literalText('42'), '=42');
  assert.equal(m.literalText('hello'), 'hello');
});

test('fields rows round-trip an object input', () => {
  const input = { userId: 'me', n: 42, s: '42', 'x.$': 'input.x', obj: { 'k.$': 'run.id' } };
  assert.deepEqual(m.rowsToInput(m.inputToRows(input)), input);
  assert.equal(m.literalTypeHint(m.inputToRows(input)[4].value), 'template');
});

test('rowsToInput keeps keys as typed, skips blank rows, last duplicate wins', () => {
  const rows = [
    { key: ' name ', value: 'Alice', kind: 'literal' },
    { key: '', value: 'pending', kind: 'literal' },
    { key: '   ', value: 'x', kind: 'expr' },
    { key: 'count', value: '1', kind: 'literal' },
    { key: 'count', value: 'input.n', kind: 'expr' },
  ];
  assert.deepEqual(m.rowsToInput(rows), { ' name ': 'Alice', 'count.$': 'input.n' });
  assert.deepEqual(m.duplicateRowKeys(rows), ['count']);
});

test('input mode is derived from the shape', () => {
  assert.equal(m.deriveInputMode({ name: 'a', type: 'function', 'input.$': 'x' }), 'expression');
  assert.equal(m.deriveInputMode({ name: 'a', type: 'function', input: { a: { b: 1 } } }), 'fields');
  assert.equal(m.deriveInputMode({ name: 'a', type: 'function', input: [1] }), 'json');
  assert.equal(m.deriveInputMode({ name: 'a', type: 'function' }), 'fields');
});

test('schema helpers tolerate boolean subschemas and fill defaults', () => {
  const schema = {
    type: 'object',
    properties: { anything: true, n: { type: 'integer' }, mode: { enum: ['a', 'b'] }, flag: { type: 'boolean', default: true } },
    required: ['n'],
  };
  assert.deepEqual(m.exampleInput(schema), { anything: '', n: 0, mode: 'a', flag: true });
  assert.deepEqual(m.inputSchemaFields(schema).map((f) => [f.name, f.required]), [
    ['anything', false], ['n', true], ['mode', false], ['flag', false],
  ]);
});

test('expression suggestions list run input fields and earlier steps only', () => {
  const steps = [
    { name: 'fetch', type: 'connector' },
    { name: 'enrich', type: 'function' },
    { name: 'store', type: 'load' },
  ];
  const at = (i) => m.expressionSuggestions(steps, i, { properties: { fail: {}, 'odd key': {} } });
  assert.deepEqual(at(0).map((g) => g.label), ['Run input', 'Cursor & run']);
  assert.deepEqual(at(0)[0].options, ['input', 'input.fail', 'input."odd key"']);
  assert.deepEqual(at(2)[1].options, ['steps.fetch.output', 'steps.fetch.output.body', 'steps.fetch.output.statusCode', 'steps.enrich.output']);
});
