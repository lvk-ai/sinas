// Tests for console/src/lib/apiKeyEdit.ts: the PATCH /api-keys/{id} body
// (only what changed) and who is offered Edit.
//
// Run: npm test (node:test; the module is transpiled with esbuild).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const entry = fileURLToPath(new URL('../src/lib/apiKeyEdit.ts', import.meta.url));
const { outputFiles } = await build({ entryPoints: [entry], bundle: true, format: 'esm', write: false, logLevel: 'silent' });
const m = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString('base64')}`);

const key = {
  name: 'ci',
  permissions: { 'sinas.agents/*/*.read:own': true, 'sinas.functions.read:own': true },
  roles: [{ id: 'r1', name: 'Deployers' }, { id: 'r2', name: 'Readers' }],
};

test('the draft starts as the key, detached from it', () => {
  const draft = m.draftFromKey(key);
  assert.deepEqual(draft, {
    name: 'ci',
    permissions: { 'sinas.agents/*/*.read:own': true, 'sinas.functions.read:own': true },
    role_ids: ['r1', 'r2'],
  });
  draft.permissions.x = true;
  draft.role_ids.push('r3');
  assert.equal(key.permissions.x, undefined);
  assert.equal(key.roles.length, 2);
  assert.deepEqual(m.draftFromKey({ name: 'bare', permissions: {} }), { name: 'bare', permissions: {}, role_ids: [] });
});

test('nothing changed: an empty body', () => {
  const payload = m.apiKeyUpdatePayload(key, m.draftFromKey(key));
  assert.deepEqual(payload, {});
  assert.equal(m.isEmptyUpdate(payload), true);
  // Order does not count as a change.
  const reordered = {
    name: 'ci',
    permissions: { 'sinas.functions.read:own': true, 'sinas.agents/*/*.read:own': true },
    role_ids: ['r2', 'r1'],
  };
  assert.deepEqual(m.apiKeyUpdatePayload(key, reordered), {});
});

test('only the changed fields are sent', () => {
  const draft = m.draftFromKey(key);
  assert.deepEqual(m.apiKeyUpdatePayload(key, { ...draft, name: '  ci-prod ' }), { name: 'ci-prod' });

  const perms = { ...draft.permissions, 'sinas.logs.read:own': true };
  assert.deepEqual(m.apiKeyUpdatePayload(key, { ...draft, permissions: perms }), { permissions: perms });

  assert.deepEqual(m.apiKeyUpdatePayload(key, { ...draft, role_ids: ['r1'] }), { role_ids: ['r1'] });
  assert.equal(m.isEmptyUpdate({ role_ids: [] }), false);
});

test('clearing permissions or roles sends the empty set (it replaces)', () => {
  const draft = m.draftFromKey(key);
  assert.deepEqual(m.apiKeyUpdatePayload(key, { ...draft, permissions: {}, role_ids: [] }), {
    permissions: {},
    role_ids: [],
  });
});

test('a value flip counts; a blank or untouched name is left out', () => {
  const draft = m.draftFromKey(key);
  const flipped = { ...draft.permissions, 'sinas.functions.read:own': false };
  assert.deepEqual(m.apiKeyUpdatePayload(key, { ...draft, permissions: flipped }), { permissions: flipped });
  assert.deepEqual(m.apiKeyUpdatePayload(key, { ...draft, name: '   ' }), {});
  assert.deepEqual(m.apiKeyUpdatePayload(key, { ...draft, name: ' ci ' }), {});
});

test('duplicate role ids are sent once', () => {
  const draft = m.draftFromKey(key);
  assert.deepEqual(m.apiKeyUpdatePayload(key, { ...draft, role_ids: ['r3', 'r3'] }), { role_ids: ['r3'] });
});

test('Edit is offered as PATCH would allow it', () => {
  const own = { user_id: 'u1', is_active: true };
  const other = { user_id: 'u2', is_active: true };
  const revoked = { user_id: 'u1', is_active: false };
  const none = { userId: 'u1', updateAll: false, updateOwn: false };
  const ownOnly = { userId: 'u1', updateAll: false, updateOwn: true };
  const all = { userId: 'u1', updateAll: true, updateOwn: true };

  assert.equal(m.canEditAPIKey(own, none), false);
  assert.equal(m.canEditAPIKey(own, ownOnly), true);
  assert.equal(m.canEditAPIKey(other, ownOnly), false);
  assert.equal(m.canEditAPIKey(other, all), true);
  // A revoked key stays revoked: nothing to edit, whoever asks.
  assert.equal(m.canEditAPIKey(revoked, all), false);
  // Unknown user: :own can't be matched.
  assert.equal(m.canEditAPIKey(own, { ...ownOnly, userId: null }), false);
  assert.equal(m.canEditAPIKey(own, { ...all, userId: undefined }), true);
});

test('linked roles the list leaves out are still offered', () => {
  const listed = [{ id: 'r1', name: 'Deployers', description: 'CI' }];
  assert.deepEqual(m.rolesForPicker(listed, key.roles), [
    { id: 'r1', name: 'Deployers', description: 'CI' },
    { id: 'r2', name: 'Readers', description: null },
  ]);
  assert.deepEqual(m.rolesForPicker(listed, undefined), listed);
  assert.deepEqual(m.rolesForPicker([], key.roles).map((r) => r.id), ['r1', 'r2']);
});
