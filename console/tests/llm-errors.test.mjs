// Tests for console/src/lib/llmErrors.ts: what the chat shows for a stream
// `error` event (the error envelope of a failed turn).
//
// Run: npm test (node:test; the module is transpiled with esbuild).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const entry = fileURLToPath(new URL('../src/lib/llmErrors.ts', import.meta.url));
const { outputFiles } = await build({ entryPoints: [entry], bundle: true, format: 'esm', write: false, logLevel: 'silent' });
const m = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString('base64')}`);

test('the message of an envelope, with the hint for its code', () => {
  const data = JSON.stringify({ error: 'rate_limited: slow down', code: 'rate_limited', retryable: true });
  assert.equal(
    m.streamErrorMessage(data),
    'rate_limited: slow down\n\nThe model provider is rate limiting requests; try again in a moment.',
  );
});

test('a code whose message says what to do gets no hint', () => {
  const error = 'provider_not_configured: No LLM provider is configured. An admin can add one under LLM Providers.';
  assert.equal(m.streamErrorMessage(JSON.stringify({ error, code: 'provider_not_configured' })), error);
  assert.equal(m.errorHint('provider_not_configured'), null);
  assert.equal(m.errorHint(undefined), null);
});

test('older and odd payloads', () => {
  assert.equal(m.streamErrorMessage(JSON.stringify({ error: 'boom' })), 'boom');
  assert.equal(m.streamErrorMessage(JSON.stringify({ detail: 'internal_error: x', code: 'internal_error' })), 'internal_error: x');
  assert.equal(m.streamErrorMessage('plain text'), 'plain text');
  assert.equal(m.streamErrorMessage(''), 'Stream error');
  assert.equal(m.streamErrorMessage(JSON.stringify({})), 'Stream error');
});
