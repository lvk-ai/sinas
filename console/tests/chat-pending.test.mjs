// Tests for console/src/lib/chatPending.ts: ask_user questions (API rows and
// stream events), when an approval or an answer resumes the stream, and how
// an approval card names the call.
//
// Run: npm test (node:test; the module is transpiled with esbuild).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const entry = fileURLToPath(new URL('../src/lib/chatPending.ts', import.meta.url));
const { outputFiles } = await build({ entryPoints: [entry], bundle: true, format: 'esm', write: false, logLevel: 'silent' });
const m = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString('base64')}`);

test('pending-input rows map to questions; rows without a tool call id are dropped', () => {
  const rows = [
    { tool_call_id: 'tc1', question: 'Which env?', options: ['dev', 'prod'], expires_at: '2026-10-20T10:00:00Z', created_at: 'x' },
    { tool_call_id: 'tc2', question: 'Why?', options: null, expires_at: null },
    { question: 'orphan' },
    null,
  ];
  assert.deepEqual(m.toPendingInputs(rows), [
    { toolCallId: 'tc1', question: 'Which env?', options: ['dev', 'prod'], expiresAt: '2026-10-20T10:00:00Z' },
    { toolCallId: 'tc2', question: 'Why?', options: null, expiresAt: null },
  ]);
  assert.deepEqual(m.toPendingInputs(undefined), []);
});

test('options that are not a list of strings are ignored, not shown', () => {
  assert.equal(m.toPendingInput({ tool_call_id: 't', question: 'q', options: [1, 2] }).options, null);
  assert.equal(m.toPendingInput({ tool_call_id: 't', question: 'q', options: [] }).options, null);
});

test('a question arrives as its own event (queued turn, reconnect)', () => {
  const data = JSON.stringify({ type: 'input_required', tool_call_id: 'tc1', question: 'Proceed?', options: ['yes', 'no'], expires_at: null });
  assert.deepEqual(m.inputRequiredFromChunk({ event: 'input_required', data }), {
    toolCallId: 'tc1', question: 'Proceed?', options: ['yes', 'no'], expiresAt: null,
  });
});

test('a question arrives as a typed message event (inline turn)', () => {
  const data = JSON.stringify({ type: 'input_required', tool_call_id: 'tc9', question: 'Name?' });
  assert.equal(m.inputRequiredFromChunk({ event: 'message', data }).toolCallId, 'tc9');
});

test('ordinary chunks are not questions', () => {
  assert.equal(m.inputRequiredFromChunk({ event: 'message', data: JSON.stringify({ content: 'hi' }) }), null);
  assert.equal(m.inputRequiredFromChunk({ event: 'message', data: 'not json' }), null);
  assert.equal(m.inputRequiredFromChunk({ event: 'message', data: 'null' }), null);
  assert.equal(m.inputRequiredFromChunk({ event: 'round_suspended', data: JSON.stringify({ tool_call_ids: ['tc1'] }) }), null);
  assert.equal(m.inputRequiredFromChunk({ event: 'tool_start', data: JSON.stringify({ type: 'input_required', tool_call_id: 'x' }) }), null);
});

test('a question already listed is not added twice (event, then reload)', () => {
  const a = { toolCallId: 'a', question: 'A', options: null, expiresAt: null };
  const b = { toolCallId: 'b', question: 'B', options: null, expiresAt: null };
  const current = [a];
  assert.equal(m.mergePendingInputs(current, [a]), current);
  assert.deepEqual(m.mergePendingInputs(current, [a, b, b]), [a, b]);
});

test('an approval round streams only after its last decision', () => {
  assert.equal(m.approvalResumeChannel({ resume: 'waiting', channel_id: 'ch' }), null);
  assert.equal(m.approvalResumeChannel({ resume: 'queued', channel_id: 'ch' }), 'ch');
  assert.equal(m.approvalResumeChannel({ resume: 'queued', channel_id: null }), null);
});

test('an answer streams when it resumed the round, or the resume is being retried', () => {
  assert.equal(m.answerResumeChannel({ resumed: true, resume_pending: false, channel_id: 'ch' }), 'ch');
  assert.equal(m.answerResumeChannel({ resumed: false, resume_pending: true, channel_id: 'ch' }), 'ch');
  assert.equal(m.answerResumeChannel({ resumed: false, resume_pending: false, channel_id: null }), null);
});

test('approval cards name functions, platform tools and other tools apart', () => {
  assert.deepEqual(m.approvalSubject('billing', 'refund'), { kind: 'Function', name: 'billing/refund' });
  assert.deepEqual(m.approvalSubject('sinas', 'sinas_package_install'), { kind: 'Platform tool', name: 'sinas_package_install' });
  assert.deepEqual(m.approvalSubject('tool', 'workbench_promote'), { kind: 'Tool', name: 'workbench_promote' });
});
