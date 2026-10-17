// Tests for console/src/lib/setupLink.ts parseSetupLink: which console URLs
// are setup links, and the address the token is replaced with.
//
// Run: npm test (node:test; the module is transpiled with esbuild).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const entry = fileURLToPath(new URL('../src/lib/setupLink.ts', import.meta.url));
const { outputFiles } = await build({ entryPoints: [entry], bundle: true, format: 'esm', write: false, logLevel: 'silent' });
const m = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString('base64')}`);

test('the short form and its variants are setup links', () => {
  for (const path of ['/ui', '/ui/', '/ui/setup', '/ui/setup/', '/ui/reset-password']) {
    assert.deepEqual(m.parseSetupLink(path, '?token=abc', '', '/ui/'), { token: 'abc', cleanUrl: '/ui/setup' }, path);
  }
});

test('the token is removed, other parameters and the hash kept', () => {
  assert.deepEqual(m.parseSetupLink('/ui/', '?x=1&token=abc&y=2', '#h', '/ui'), {
    token: 'abc',
    cleanUrl: '/ui/setup?x=1&y=2#h',
  });
});

test('url-encoded tokens are decoded', () => {
  assert.equal(m.parseSetupLink('/ui/', '?token=a%2Db_c', '', '/ui/').token, 'a-b_c');
});

test('not a setup link: other pages, no token, an empty token, outside the console', () => {
  assert.equal(m.parseSetupLink('/ui/agents', '?token=abc', '', '/ui/'), null);
  assert.equal(m.parseSetupLink('/ui/shared/xyz', '?token=abc', '', '/ui/'), null);
  assert.equal(m.parseSetupLink('/ui/', '', '', '/ui/'), null);
  assert.equal(m.parseSetupLink('/ui/', '?token=', '', '/ui/'), null);
  assert.equal(m.parseSetupLink('/uix', '?token=abc', '', '/ui/'), null);
  assert.equal(m.parseSetupLink('/', '?token=abc', '', '/ui/'), null);
});
