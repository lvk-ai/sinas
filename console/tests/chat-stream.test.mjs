// Tests for console/src/lib/chatStream.ts: the console chat session's state
// transitions (streamed text and tool bubbles, approval cards, ordering of
// refreshes, polling while a question waits).
//
// Run: npm test (node:test; the module is transpiled with esbuild).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const entry = fileURLToPath(new URL('../src/lib/chatStream.ts', import.meta.url));
const { outputFiles } = await build({ entryPoints: [entry], bundle: true, format: 'esm', write: false, logLevel: 'silent' });
const m = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString('base64')}`);

const now = new Date('2026-10-19T12:00:00Z');

test('message events: text is taken, typed events carry none', () => {
  assert.equal(m.textFromMessageChunk(JSON.stringify({ content: 'hi' })), 'hi');
  assert.equal(m.textFromMessageChunk(JSON.stringify({ type: 'tool_rejected', tool_call_id: 'x' })), '');
  assert.equal(m.textFromMessageChunk(JSON.stringify({ content: { parts: [] } })), '');
  assert.equal(m.textFromMessageChunk('null'), '');
  assert.equal(m.textFromMessageChunk('plain'), 'plain');
});

test('streamed text grows one assistant message, then a new one after a tool bubble', () => {
  let msgs = [];
  msgs = m.applyTextDelta(msgs, 'Hel', 'c1', now);
  msgs = m.applyTextDelta(msgs, 'lo', 'c1', now);
  assert.equal(msgs.length, 1);
  assert.equal(msgs[0].content, 'Hello');
  assert.equal(msgs[0].streaming, true);

  msgs = m.applyToolStart(msgs, { tool_call_id: 't1', name: 'ask_user', arguments: '{"q":1}' }, 'c1', now);
  assert.equal(msgs.length, 2);
  assert.equal(msgs[0].streaming, false, 'text before the tool is frozen');
  assert.deepEqual(msgs[1].toolCalls[0].function, { name: 'ask_user', arguments: '{"q":1}' });

  msgs = m.applyTextDelta(msgs, 'After', 'c1', now);
  assert.equal(msgs.length, 3);
  assert.equal(msgs[2].content, 'After');
  assert.equal(m.applyTextDelta(msgs, '', 'c1', now), msgs, 'empty text changes nothing');
});

test('a tool start is shown once; its result lands in the same bubble', () => {
  let msgs = m.applyToolStart([], { tool_call_id: 't1', name: 'x' }, 'c1', now);
  assert.equal(m.applyToolStart(msgs, { tool_call_id: 't1', name: 'x' }, 'c1', now), msgs);
  msgs = m.applyToolEnd(msgs, { tool_call_id: 't1', name: 'x', result: { ok: true } });
  assert.deepEqual(msgs[0].toolResults, { t1: { content: '{"ok":true}', name: 'x' } });
  assert.equal(m.applyToolEnd(msgs, { tool_call_id: 't1', name: 'x' }), msgs, 'no result, no change');
});

test('approval events become cards, once per call', () => {
  const a = m.approvalFromEvent(JSON.stringify({
    type: 'approval_required', tool_call_id: 'c1', function_namespace: 'tool', function_name: 'workbench_promote', arguments: { path: 'x' },
  }));
  assert.deepEqual(a, { type: 'approval_required', toolCallId: 'c1', functionNamespace: 'tool', functionName: 'workbench_promote', arguments: { path: 'x' } });
  assert.equal(m.approvalFromEvent('nope'), null);
  assert.equal(m.approvalFromEvent(JSON.stringify({ type: 'approval_required' })), null);
  const cards = m.addApproval([], a);
  assert.equal(m.addApproval(cards, { ...a }), cards);
});

test('only the newest refresh may write: an older one that lands late is dropped', () => {
  const gate = m.createLatestGate();
  const first = gate.next();   // turn 1 ends, its refresh starts
  const turn2 = gate.next();   // the user sends again before it lands
  assert.equal(gate.isCurrent(first), false);
  const second = gate.next();  // turn 2 ends, its refresh starts
  assert.equal(gate.isCurrent(turn2), false);
  assert.equal(gate.isCurrent(second), true);
});

test('while a question waits, re-read at most every 30s and just after its deadline', () => {
  const t = now.getTime();
  const q = (expiresAt) => ({ toolCallId: 'q', question: '?', options: null, expiresAt });
  assert.equal(m.nextPendingPollMs([q(null)], t), m.PENDING_POLL_MAX_MS);
  assert.equal(m.nextPendingPollMs([q(new Date(t + 3_600_000).toISOString())], t), m.PENDING_POLL_MAX_MS);
  assert.equal(m.nextPendingPollMs([q(new Date(t + 10_000).toISOString())], t), 15_000);
  assert.equal(m.nextPendingPollMs([q(new Date(t - 60_000).toISOString())], t), 10_000, 'overdue: wait for the sweep');
  assert.equal(m.nextPendingPollMs([q('not a date')], t), m.PENDING_POLL_MAX_MS);
});
