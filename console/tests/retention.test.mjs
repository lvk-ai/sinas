// Tests for console/src/lib/retention.ts: the agent form's retention field
// (choice <-> retention_days) and the Retention panel's wording.
//
// Run: npm test (node:test; the module is transpiled with esbuild).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const entry = fileURLToPath(new URL('../src/lib/retention.ts', import.meta.url));
const { outputFiles } = await build({ entryPoints: [entry], bundle: true, format: 'esm', write: false, logLevel: 'silent' });
const m = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString('base64')}`);

test('stored value to choice: null is the default, 0 is keep forever, N is days', () => {
  assert.equal(m.retentionMode(null), 'default');
  assert.equal(m.retentionMode(undefined), 'default');
  assert.equal(m.retentionMode(0), 'never');
  assert.equal(m.retentionMode(30), 'days');
});

test('choice to the value sent: default sends null (clears), never sends 0', () => {
  assert.equal(m.retentionDaysFor('default', '45'), null);
  assert.equal(m.retentionDaysFor('never', '45'), 0);
  assert.equal(m.retentionDaysFor('days', ' 45 '), 45);
});

test('days that are not a whole number in range are not sent', () => {
  for (const bad of ['', '0', '-3', '1.5', 'abc', '36501', '1e3']) {
    assert.equal(m.retentionDaysFor('days', bad), undefined, bad);
    assert.equal(m.parseRetentionDays(bad), null, bad);
  }
  assert.equal(m.parseRetentionDays('36500'), 36500);
  assert.equal(m.parseRetentionDays('1'), 1);
});

test('the default option names the default only when it is known', () => {
  assert.equal(m.defaultOptionLabel(90, true), 'Use default (90 days)');
  assert.equal(m.defaultOptionLabel(1, true), 'Use default (1 day)');
  assert.equal(m.defaultOptionLabel(null, true), 'Use default (keep forever)');
  assert.equal(m.defaultOptionLabel(90, false), 'Use default');
});

test('describing a setting', () => {
  assert.equal(m.describeRetention(0, 90), 'Keep forever');
  assert.equal(m.describeRetention(14, 90), '14 days');
  assert.equal(m.describeRetention(null, 90), 'Default (90 days)');
  assert.equal(m.describeRetention(null, null), 'Default (keep forever)');
  assert.equal(m.describeRetention(null, 90, false), 'Deployment default');
});

test('run summary', () => {
  assert.equal(m.runSummary(null), 'No chats handled');
  assert.equal(
    m.runSummary({ archived: 12, deleted: 11, messages_deleted: 340, skipped: { running: 2, parked: 1 }, failed: { upload: 1 }, more_remaining: true }),
    '12 archived, 11 deleted (340 messages); 3 skipped, 1 failed, more left for the next run',
  );
  assert.equal(
    m.runSummary({ archived: 1, deleted: 1, messages_deleted: 1, skipped: {}, failed: {} }),
    '1 archived, 1 deleted (1 message)',
  );
});

test('only a value the user changed is validated (a stored one is sent back as is)', () => {
  assert.equal(m.retentionInputError('days', '40000', false), null, 'loaded from the API, untouched');
  assert.match(m.retentionInputError('days', '40000', true), /1 to 36,500/);
  assert.match(m.retentionInputError('days', '', true), /whole number/);
  assert.equal(m.retentionInputError('days', '30', true), null);
  assert.equal(m.retentionInputError('default', '', true), null);
  assert.equal(m.retentionInputError('never', 'x', true), null);
});

test('run limits are mentioned only when they matter', () => {
  const none = { max_chats_per_run: null, max_run_seconds: null };
  assert.equal(m.runLimitNote(none, 500), null);
  assert.equal(m.runLimitNote({ max_chats_per_run: 100, max_run_seconds: null }, 40), null, 'everything fits');
  assert.equal(
    m.runLimitNote({ max_chats_per_run: 100, max_run_seconds: null }, 400),
    'One run handles up to 100 chats; whatever is left goes in later runs.',
  );
  assert.equal(
    m.runLimitNote({ max_chats_per_run: 100, max_run_seconds: 3300 }, 4),
    'One run handles up to 100 chats, for at most 55 minutes; whatever is left goes in later runs.',
  );
  assert.match(m.runLimitNote({ max_chats_per_run: 100, max_run_seconds: null }), /up to 100 chats/, 'count unknown');
});

test('the preview reports eligible chats, and a refusal instead of promising deletions', () => {
  const totals = { chats: 4, messages: 16 };
  assert.equal(
    m.previewSummary(totals, true, null),
    '4 expired chats (16 messages) are past their retention period. A run archives each, then deletes it.',
  );
  const refused = m.previewSummary(totals, false, 'Chat retention is disabled.');
  assert.match(refused, /^4 expired chats .* but a run now would be refused: Chat retention is disabled\.$/);
  assert.doesNotMatch(refused, /delet/);
  assert.match(m.previewSummary({ chats: 1, messages: 1 }, true, null), /^1 expired chat \(1 message\) is past its/);
});

test('the confirmation calls the counts eligible and states the run limits', () => {
  const limits = { max_chats_per_run: 100, max_run_seconds: null };
  const msg = m.confirmRunMessage({ chats: 400, messages: 9000 }, true, limits);
  assert.match(msg, /Eligible as of the last preview: 400 chats \(9,000 messages\)\./);
  assert.match(msg, /archived and then permanently deleted/);
  assert.match(msg, /up to 100 chats; whatever is left goes in later runs/);
  assert.doesNotMatch(m.confirmRunMessage({ chats: 3, messages: 9 }, true, limits), /later runs/);
  assert.match(m.confirmRunMessage(null, false, limits), /Every chat past its retention period is eligible\.[\s\S]*no archive destination/);
});

test('skip reasons and sizes read as words', () => {
  assert.equal(m.skipLabel('pending_approval'), 'tool approval pending');
  assert.equal(m.skipLabel('something_new'), 'something new');
  assert.equal(m.skipLabel('too_large'), 'too large to archive safely');
  assert.equal(m.skipLabel('not_eligible'), 'active again or retention changed');
  assert.equal(m.formatBytes(512), '512 B');
  assert.equal(m.formatBytes(1536), '1.5 KB');
  assert.equal(m.formatBytes(50 * 1024 * 1024), '50 MB');
});
