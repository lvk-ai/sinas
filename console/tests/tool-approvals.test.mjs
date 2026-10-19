// Tests for console/src/lib/toolApprovals.ts: the agent editor's permission
// mode (tool_approvals) — load, validate, the value sent on save, reorder.
//
// Run: npm test (node:test; the module is transpiled with esbuild).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const entry = fileURLToPath(new URL('../src/lib/toolApprovals.ts', import.meta.url));
const { outputFiles } = await build({ entryPoints: [entry], bundle: true, format: 'esm', write: false, logLevel: 'silent' });
const m = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString('base64')}`);

test('an unset or malformed value loads as the backend default: run, no rules', () => {
  for (const v of [null, undefined, 'ask', [], 42]) {
    assert.deepEqual(m.toolApprovalsDraft(v), { default: 'auto', rules: [] }, String(v));
  }
  assert.deepEqual(m.toolApprovalsDraft({ default: 'bogus', rules: 'x' }), { default: 'auto', rules: [] });
});

test('a stored value loads as is, rule order kept', () => {
  const stored = {
    default: 'ask',
    rules: [
      { match: 'workbench_promote', action: 'ask' },
      { match: 'sinas_package_*', action: 'auto' },
    ],
  };
  assert.deepEqual(m.toolApprovalsDraft(stored), stored);
});

test('a rule without a pattern blocks saving, naming the rule', () => {
  const draft = { default: 'auto', rules: [{ match: 'a*', action: 'ask' }, { match: '  ', action: 'auto' }] };
  assert.equal(m.toolApprovalsError(draft), 'Rule 2 needs a tool name pattern.');
  assert.equal(m.toolApprovalsError({ default: 'auto', rules: [{ match: 'a*', action: 'ask' }] }), null);
  assert.equal(m.toolApprovalsError({ default: 'ask', rules: [] }), null);
});

test('an agent without a setting saves without one while the draft means "unset"', () => {
  assert.equal(m.toolApprovalsPayload({ default: 'auto', rules: [] }, null), undefined);
  assert.equal(m.toolApprovalsPayload({ default: 'auto', rules: [] }, undefined), undefined);
});

test('any real setting is sent in the shape the backend validates, patterns trimmed', () => {
  assert.deepEqual(m.toolApprovalsPayload({ default: 'ask', rules: [] }, null), { default: 'ask', rules: [] });
  assert.deepEqual(
    m.toolApprovalsPayload({ default: 'auto', rules: [{ match: ' mcp_* ', action: 'ask' }] }, null),
    { default: 'auto', rules: [{ match: 'mcp_*', action: 'ask' }] },
  );
});

test('clearing a stored setting sends the empty form (the API ignores null)', () => {
  const stored = { default: 'ask', rules: [{ match: 'x', action: 'ask' }] };
  assert.deepEqual(m.toolApprovalsPayload({ default: 'auto', rules: [] }, stored), { default: 'auto', rules: [] });
});

test('moving a rule swaps it with its neighbour and stops at the ends', () => {
  const rules = [
    { match: 'a', action: 'ask' },
    { match: 'b', action: 'auto' },
    { match: 'c', action: 'ask' },
  ];
  assert.deepEqual(m.moveRule(rules, 1, -1).map((r) => r.match), ['b', 'a', 'c']);
  assert.deepEqual(m.moveRule(rules, 1, 1).map((r) => r.match), ['a', 'c', 'b']);
  assert.equal(m.moveRule(rules, 0, -1), rules);
  assert.equal(m.moveRule(rules, 2, 1), rules);
  assert.deepEqual(rules.map((r) => r.match), ['a', 'b', 'c'], 'input not mutated');
});
