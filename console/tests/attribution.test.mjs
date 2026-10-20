// Tests for console/src/lib/attribution.ts: an LLM provider's header
// templates (rows <-> config.headers, and what the backend would refuse) and
// an identity provider's claim mappings.
//
// Run: npm test (node:test; the module is transpiled with esbuild).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const entry = fileURLToPath(new URL('../src/lib/attribution.ts', import.meta.url));
const { outputFiles } = await build({ entryPoints: [entry], bundle: true, format: 'esm', write: false, logLevel: 'silent' });
const m = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString('base64')}`);

test('header rows round-trip through the config, other keys kept', () => {
  const config = { propagate_context: true, headers: { 'x-customer-id': '{{user.custom_fields.orgId}}' } };
  const rows = m.headerRowsFromConfig(config);
  assert.deepEqual(rows, [{ name: 'x-customer-id', template: '{{user.custom_fields.orgId}}' }]);
  assert.deepEqual(m.configWithHeaders(config, rows), config);
  assert.deepEqual(m.configWithHeaders(config, []), { propagate_context: true });
  // An empty row is dropped; names are trimmed.
  assert.deepEqual(
    m.configWithHeaders({}, [{ name: ' x-a ', template: 'v' }, { name: '', template: '' }]),
    { headers: { 'x-a': 'v' } },
  );
  assert.deepEqual(m.headerRowsFromConfig({ headers: ['x'] }), []);
  assert.deepEqual(m.headerRowsFromConfig(null), []);
});

test('header rows: what the backend refuses is pointed out', () => {
  const problems = m.headerRowProblems([
    { name: 'x-customer-id', template: '{{user.custom_fields.orgId}}' },
    { name: 'x-tags', template: 'agent:{{ agent.namespace }}/{{agent.name}}' },
    { name: 'Authorization', template: 'Bearer x' },
    { name: 'traceparent', template: '{{chat.id}}' },
    { name: 'anthropic-beta', template: 'x' },
    { name: 'bad name', template: 'x' },
    { name: 'X-Customer-Id', template: 'again' },
    { name: 'x-unknown', template: '{{request.headers.x}}' },
    { name: 'x-key', template: '{{user.custom_fields.a-b}}' },
    { name: 'x-open', template: '{{user.id' },
    { name: 'x-empty', template: ' ' },
    { name: 'x-break', template: 'a\nb' },
    { name: '', template: '' },
  ]);
  assert.equal(problems[0], null);
  assert.equal(problems[1], null);
  assert.match(problems[2], /Reserved/);
  assert.match(problems[3], /Reserved/);
  assert.match(problems[4], /Reserved/);
  assert.match(problems[5], /letters, digits/);
  assert.match(problems[6], /twice/);
  assert.match(problems[7], /Unknown variable/);
  assert.match(problems[8], /field key/);
  assert.match(problems[9], /Unbalanced/);
  assert.match(problems[10], /Enter a template/);
  assert.match(problems[11], /Printable/);
  assert.equal(problems[12], null);
});

test('every documented variable is accepted', () => {
  const rows = m.HEADER_TEMPLATE_VARIABLES.map((v, i) => ({
    name: `x-v${i}`,
    template: v.name.replace('<key>', 'orgId'),
  }));
  assert.deepEqual(m.headerRowProblems(rows), rows.map(() => null));
});

test('claim mappings: what the backend refuses is pointed out', () => {
  const problems = m.claimMappingProblems([
    { claim: 'org_id', field: 'orgId' },
    { claim: 'https://example.com/plan', field: 'plan' },
    { claim: '', field: 'x' },
    { claim: 'org', field: '' },
    { claim: 'org', field: 'org-id' },
    { claim: 'other', field: 'orgId' },
    { claim: '.org', field: 'y' },
  ]);
  assert.deepEqual(problems.slice(0, 2), [null, null]);
  assert.match(problems[2], /claim/);
  assert.match(problems[3], /custom field/);
  assert.match(problems[4], /letters, digits/);
  assert.match(problems[5], /twice/);
  assert.match(problems[6], /claim path/);
});
