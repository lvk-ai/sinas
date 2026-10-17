// Tests for console/src/lib/clickhouse.ts — which state the request-log view
// shows when ClickHouse is disabled (`features.clickhouse: false` in /info)
// or unreachable (503 from /request-logs).
//
// Run: npm test (node:test; the module is transpiled with esbuild).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const entry = fileURLToPath(new URL('../src/lib/clickhouse.ts', import.meta.url));
const { outputFiles } = await build({ entryPoints: [entry], bundle: true, format: 'esm', write: false, logLevel: 'silent' });
const m = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString('base64')}`);

const http = (status, detail) => ({ response: { status, data: detail === undefined ? {} : { detail } } });

test('only an explicit false disables: a missing flag or /info counts as enabled', () => {
  assert.equal(m.isClickHouseEnabled({ clickhouse: false }), false);
  assert.equal(m.isClickHouseEnabled({ clickhouse: true }), true);
  assert.equal(m.isClickHouseEnabled({}), true);
  assert.equal(m.isClickHouseEnabled(undefined), true);
  assert.equal(m.isClickHouseEnabled(null), true);
});

test('disabled wins over any error: the explanation, not a failure', () => {
  assert.equal(m.requestLogsState({ clickhouse: false }, null), 'disabled');
  assert.equal(m.requestLogsState({ clickhouse: false }, http(503)), 'disabled');
});

test('a 503 from an enabled instance is unavailable; other errors are errors', () => {
  assert.equal(m.requestLogsState({ clickhouse: true }, null), 'ok');
  assert.equal(m.requestLogsState({ clickhouse: true }, http(503, 'down')), 'unavailable');
  assert.equal(m.requestLogsState({}, http(503)), 'unavailable');
  assert.equal(m.requestLogsState({ clickhouse: true }, http(500)), 'error');
  assert.equal(m.requestLogsState({ clickhouse: true }, new Error('network')), 'error');
});

test('unavailableDetail shows the backend explanation, with a fallback', () => {
  assert.equal(m.unavailableDetail(http(503, 'ClickHouse could not be reached.')), 'ClickHouse could not be reached.');
  assert.match(m.unavailableDetail(http(503)), /temporarily unavailable/);
  assert.match(m.unavailableDetail(undefined), /temporarily unavailable/);
});

test('the docs link points at the ClickHouse env var section', () => {
  assert.equal(m.CLICKHOUSE_ENV_VAR, 'CLICKHOUSE_HOST');
  assert.match(m.CLICKHOUSE_DOCS_URL, /environment-variables#clickhouse/);
});
