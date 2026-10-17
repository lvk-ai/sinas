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

// A minimal sessionStorage for the resume tests (node has none).
function stubSessionStorage() {
  const store = new Map();
  globalThis.sessionStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
  };
  return store;
}

const session = { setup_token: 's', email: 'a@example.com', has_password: false, expires_at: '2100-01-01T00:00:00Z' };

test('a reload during the code step resumes the code step, not "invalid link"', () => {
  stubSessionStorage();
  // password+otp: the password is saved, the setup session cleared, a code sent
  m.saveSetupSession(session);
  m.clearSetupSession();
  m.savePendingCode({ session_id: 'otp-1', email: 'a@example.com' });
  assert.deepEqual(m.resumeSetup(), { step: 'code', pending: { session_id: 'otp-1', email: 'a@example.com' } });
  m.clearPendingCode();
  assert.equal(m.resumeSetup(), null);
});

test('a reload before saving resumes the password step until the session expires', () => {
  stubSessionStorage();
  m.saveSetupSession(session);
  assert.deepEqual(m.resumeSetup(Date.parse('2099-01-01T00:00:00Z')), { step: 'password', session });
  assert.equal(m.resumeSetup(Date.parse('2100-01-02T00:00:00Z')), null);
});

test('garbage in storage resumes nothing', () => {
  const store = stubSessionStorage();
  store.set('sinas_setup_pending_code', '{not json');
  store.set('sinas_setup_session', '{"email":"x"}');
  assert.equal(m.resumeSetup(), null);
});

test('a failed save keeps the setup session unless the server spent or refused it', () => {
  assert.equal(m.completeFailure(422), 'fix');
  // Nothing spent: rate limited, no response, a server error
  for (const status of [429, undefined, 500, 502, 504]) assert.equal(m.completeFailure(status), 'retry', String(status));
  // Spent or invalid: the generic 400, SSO 403, password saved but code failed (503)
  for (const status of [400, 403, 503]) assert.equal(m.completeFailure(status), 'spent', String(status));
});

test('the way to the login page signs another account out, and says so', () => {
  assert.deepEqual(m.signInAction(null, 'a@example.com'), { label: 'Sign in', signOut: false, toDashboard: false });
  assert.deepEqual(m.signInAction(undefined), { label: 'Go to sign in', signOut: false, toDashboard: false });
  assert.deepEqual(m.signInAction('b@example.com', 'a@example.com'), {
    label: 'Sign out and sign in as a@example.com', signOut: true, toDashboard: false,
  });
  assert.deepEqual(m.signInAction('b@example.com'), { label: 'Sign out and go to sign in', signOut: true, toDashboard: false });
  // Already signed in as the link's own account: nothing to switch
  assert.deepEqual(m.signInAction('A@example.com', 'a@example.com'), {
    label: 'Continue as A@example.com', signOut: false, toDashboard: true,
  });
});
