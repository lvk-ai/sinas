// Tests for console/src/lib/usage.ts: the calls drill-down's query params
// and paging, how a call reads in the table, and the number formats.
//
// Run: npm test (node:test; the module is transpiled with esbuild).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const entry = fileURLToPath(new URL('../src/lib/usage.ts', import.meta.url));
const { outputFiles } = await build({ entryPoints: [entry], bundle: true, format: 'esm', write: false, logLevel: 'silent' });
const m = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString('base64')}`);

const filters = {
  from: '2026-10-01T00:00:00.000Z',
  to: '2026-10-20T00:00:00.000Z',
  agent: ['ns/a'],
  provider: ['openai'],
  model: ['gpt-x'],
};

test('calls params carry the page filters and the page; the switches only when on', () => {
  assert.deepEqual(m.usageCallsParams(filters), { ...filters, limit: m.CALLS_PAGE_SIZE, offset: 0 });
  assert.deepEqual(m.usageCallsParams({}, { offset: 100, unpricedOnly: true, errorsOnly: true }), {
    limit: m.CALLS_PAGE_SIZE,
    offset: 100,
    unpriced_only: true,
    errors_only: true,
  });
  const off = m.usageCallsParams({}, { unpricedOnly: false, errorsOnly: false });
  assert.equal('unpriced_only' in off, false);
  assert.equal('errors_only' in off, false);
});

test('a negative offset is never sent; limit can be overridden', () => {
  assert.equal(m.usageCallsParams({}, { offset: -50 }).offset, 0);
  assert.equal(m.usageCallsParams({}, { limit: 10 }).limit, 10);
});

test('paging: range label and the previous page never go below the first call', () => {
  assert.equal(m.pageRangeLabel(0, 50), '1–50');
  assert.equal(m.pageRangeLabel(50, 7), '51–57');
  assert.equal(m.pageRangeLabel(0, 0), '');
  assert.equal(m.previousOffset(100), 50);
  assert.equal(m.previousOffset(20), 0);
  assert.equal(m.previousOffset(0), 0);
});

test('the page follows the filters: a change, and a change back, both start at the newest calls', () => {
  const all = JSON.stringify([filters, false, false]);
  const unpriced = JSON.stringify([filters, true, false]);
  let state = { scope: all, offset: 0 };
  // Page 2 under the first filters
  state = { scope: all, offset: 50 };
  assert.equal(m.callsPageFor(state, all), state, 'unchanged filters keep the page (same object)');
  // Unpriced only on: first page
  state = m.callsPageFor(state, unpriced);
  assert.deepEqual(state, { scope: unpriced, offset: 0 });
  // ...and off again, without paging in between: still the first page, not page 2
  state = m.callsPageFor(state, all);
  assert.deepEqual(state, { scope: all, offset: 0 });
});

const call = (over = {}) => ({
  id: 'c1',
  created_at: '2026-10-19T12:34:56+00:00',
  user_id: 'u1',
  user_email: 'ada@example.com',
  chat_id: null,
  message_id: null,
  agent: 'ns/a',
  source: 'chat',
  provider_name: 'openai-main',
  provider_type: 'openai',
  model: 'gpt-x',
  prompt_tokens: 12_345,
  completion_tokens: 678,
  total_tokens: 13_023,
  cache_read_tokens: 0,
  cache_write_tokens: 0,
  cost_usd: 0.0123,
  price_source: 'deployment',
  batch: false,
  streamed: true,
  latency_ms: 1530,
  error: null,
  ...over,
});

test('a priced, successful call', () => {
  const r = m.callRow(call());
  assert.equal(r.agent, 'ns/a');
  assert.equal(r.provider, 'openai-main');
  assert.equal(r.model, 'gpt-x');
  assert.equal(r.user, 'ada@example.com');
  assert.equal(r.input, '12.3K');
  assert.equal(r.output, '678');
  assert.equal(r.cached, '');
  assert.equal(r.cost, '$0.01');
  assert.equal(r.unpriced, false);
  assert.equal(r.status, 'ok');
  assert.equal(r.latency, '1.5s');
  assert.equal(r.timeTitle, '2026-10-19T12:34:56+00:00');
  assert.notEqual(r.time, '—');
});

test('an unpriced call says so instead of a dollar amount; $0 is a price', () => {
  const r = m.callRow(call({ cost_usd: null }));
  assert.equal(r.cost, 'unpriced');
  assert.equal(r.unpriced, true);
  const free = m.callRow(call({ cost_usd: 0 }));
  assert.equal(free.cost, '$0.00');
  assert.equal(free.unpriced, false);
});

test('cache tokens: read and written added up, split in the tooltip', () => {
  const r = m.callRow(call({ cache_read_tokens: 4000, cache_write_tokens: 1200 }));
  assert.equal(r.cached, '5.2K');
  assert.equal(r.cachedTitle, '4,000 read, 1,200 written');
});

test('an errored call carries its error', () => {
  const r = m.callRow(call({ error: 'rate limited' }));
  assert.equal(r.status, 'error');
  assert.equal(r.error, 'rate limited');
});

test('missing fields fall back: user id without an email, provider type, dashes', () => {
  const r = m.callRow(call({ user_email: undefined, provider_name: null, agent: null, model: null, created_at: null, latency_ms: null }));
  assert.equal(r.user, 'u1');
  assert.equal(r.provider, 'openai');
  assert.equal(r.agent, '—');
  assert.equal(r.model, '—');
  assert.equal(r.time, '—');
  assert.equal(r.latency, '');
  assert.equal(m.callRow(call({ user_id: null, user_email: null })).user, '—');
  assert.equal(m.callRow(call({ batch: true })).batch, true);
});

test('number formats', () => {
  assert.equal(m.formatUsd(null), '—');
  assert.equal(m.formatUsd(0.004), '$0.0040');
  assert.equal(m.formatUsd(1234.5), '$1,234.50');
  assert.equal(m.formatTokens(999), '999');
  assert.equal(m.formatTokens(2_500_000), '2.5M');
  assert.equal(m.formatLatency(250), '250ms');
});
