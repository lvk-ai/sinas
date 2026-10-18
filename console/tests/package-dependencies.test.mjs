// Tests for console/src/lib/packageDependencies.ts: how requirements read,
// the dependency plan's summary, and the dependencyVariables an install sends.
//
// Run: npm test (node:test; the module is transpiled with esbuild).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const entry = fileURLToPath(new URL('../src/lib/packageDependencies.ts', import.meta.url));
const { outputFiles } = await build({ entryPoints: [entry], bundle: true, format: 'esm', write: false, logLevel: 'silent' });
const m = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString('base64')}`);

const plan = (install_name, status, extra = {}) => ({
  install_name, package_name: install_name, status, constraints: [], required_by: [], requires: [], ...extra,
});

test('a requirement reads as name, range and instance', () => {
  assert.equal(m.formatRequirement({ name: 'gmail' }), 'gmail');
  assert.equal(m.formatRequirement({ name: 'gmail', version: '>=1.2,<2' }), 'gmail >=1.2,<2');
  assert.equal(m.formatRequirement({ name: 'mail', instance: 'mail-support' }), 'mail (as mail-support)');
  assert.equal(m.formatRequirement({ name: 'mail', instance: 'mail' }), 'mail', 'its own name is no instance');
});

test('the plan splits into installs, satisfied and blocking entries, in order', () => {
  const s = m.planSummary([
    plan('c', 'install'), plan('b', 'satisfied'), plan('a', 'install'),
    plan('x', 'missing'), plan('y', 'incompatible'), plan('z', 'cycle'), plan('w', 'invalid'),
  ]);
  assert.deepEqual(s.toInstall.map((e) => e.install_name), ['c', 'a']);
  assert.deepEqual(s.satisfied.map((e) => e.install_name), ['b']);
  assert.deepEqual(s.blocked.map((e) => e.install_name), ['x', 'y', 'z', 'w']);
  assert.deepEqual(m.planSummary(undefined), { toInstall: [], satisfied: [], blocked: [] });
});

test('dependency values go to the packages the plan installs, blank secrets left out', () => {
  const entries = [
    plan('gmail', 'install', { variables: [{ name: 'TOKEN', type: 'secret' }, { name: 'LABEL', type: 'text' }] }),
    plan('slack', 'install', { variables: [{ name: 'TOKEN', type: 'secret' }] }),
    plan('drive', 'satisfied'),
  ];
  const values = {
    gmail: { TOKEN: '', LABEL: 'support' },
    slack: { TOKEN: '' },
    drive: { X: 'not sent: already installed' },
    gone: { Y: 'not in the plan any more' },
  };
  assert.deepEqual(m.dependencyPayload(values, entries), { gmail: { LABEL: 'support' } });
  assert.equal(m.dependencyPayload({}, entries), undefined);
  assert.equal(m.dependencyPayload(values, undefined), undefined);
});

test('every warning of the packages installed first is shown, named after them; broad ones feed the consent', () => {
  const broad = 'Broad role permission (install requires allowBroadRolePermissions=true): role r: sinas.x';
  const entries = [
    plan('gmail', 'install', { preview: { success: true, changes: [], errors: [], warnings: ["Skipped skill 'gmail/x': edited by hand", broad] } }),
    plan('drive', 'satisfied', { preview: { success: true, changes: [], errors: [], warnings: ['not shown: nothing to install'] } }),
    plan('slack', 'install'),  // no preview (e.g. it failed before one)
  ];
  const { all, broad: consent } = m.installWarnings(entries, ['own warning', broad]);
  assert.deepEqual(all, ["gmail: Skipped skill 'gmail/x': edited by hand", `gmail: ${broad}`, 'own warning', broad]);
  assert.deepEqual(consent, [`gmail: ${broad}`, broad]);
  assert.deepEqual(m.installWarnings(undefined, undefined), { all: [], broad: [] });
});

test("a requirement's state is the server's verdict, with what is in the way", () => {
  const req = (extra) => ({ name: 'gmail', version: '>=2', install_name: 'gmail', ...extra });
  assert.deepEqual(m.requirementState(req({ status: 'satisfied', installed_version: '2.1.0' })), {
    tone: 'ok', note: 'Installed: 2.1.0',
  });
  assert.deepEqual(m.requirementState(req({ status: 'incompatible', reason: 'version', installed_version: '1.0.0' })), {
    tone: 'warn', note: "Installed 1.0.0, which doesn't satisfy >=2",
  });
  assert.deepEqual(
    m.requirementState(req({ status: 'incompatible', reason: 'other_package', installed_package: 'slack' })),
    { tone: 'warn', note: "Name taken: 'gmail' is package slack" },
  );
  assert.deepEqual(m.requirementState(req({ status: 'missing' })), { tone: 'neutral', note: 'Not installed' });
});

test('a package waiting for values to resolve its own requirements is still one to install, and gets its values', () => {
  const waiting = plan('b', 'install', { needs_input: true, requires: [], variables: [{ name: 'MAILBOX', type: 'text' }] });
  assert.deepEqual(m.planSummary([waiting]).toInstall.map((e) => e.install_name), ['b']);
  assert.deepEqual(m.planSummary([waiting]).blocked, []);
  assert.deepEqual(m.dependencyPayload({ b: { MAILBOX: 'mail-support' } }, [waiting]), { b: { MAILBOX: 'mail-support' } });
});

test('defaults pre-fill a form; absent and null defaults do not', () => {
  assert.deepEqual(
    m.variableDefaults([{ name: 'A', default: 'x' }, { name: 'B' }, { name: 'C', default: null }, { name: 'D', default: false }]),
    { A: 'x', D: false },
  );
});
