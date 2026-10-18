// Tests for console/src/lib/chatPages.ts: the GET /chats query parameters
// and keeping cached chat pages current after a create, archive or restore.
//
// Run: npm test (node:test; the module is transpiled with esbuild).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const entry = fileURLToPath(new URL('../src/lib/chatPages.ts', import.meta.url));
const { outputFiles } = await build({ entryPoints: [entry], bundle: true, format: 'esm', write: false, logLevel: 'silent' });
const m = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString('base64')}`);

const chat = (id, title = `chat ${id}`, archived = false) => ({ id, title, archived });
const pages = (...lists) => ({
  pages: lists.map((ids, i) => ({ chats: ids.map((id) => chat(id)), nextCursor: i < lists.length - 1 ? `c${i}` : null })),
  pageParams: lists.map((_, i) => (i === 0 ? null : `c${i - 1}`)),
});
const ids = (data) => data.pages.map((p) => p.chats.map((c) => c.id));
const ALL = { includeArchived: false, q: '' };

test('query parameters leave out empty filters', () => {
  assert.deepEqual(m.chatListParams({ limit: 50 }), { limit: 50 });
  assert.deepEqual(m.chatListParams({ includeArchived: false, q: '  ', limit: 50, cursor: null }), { limit: 50 });
  assert.deepEqual(m.chatListParams({ includeArchived: true, q: ' report ', limit: 20, cursor: 'abc' }), {
    include_archived: true,
    q: 'report',
    limit: 20,
    cursor: 'abc',
  });
});

test('a new chat goes to the top of the first page; later pages and cursors are untouched', () => {
  const before = pages(['a', 'b'], ['c', 'd']);
  const after = m.applyChatChange(before, chat('n'), ALL);
  assert.deepEqual(ids(after), [['n', 'a', 'b'], ['c', 'd']]);
  assert.deepEqual(after.pageParams, before.pageParams);
  assert.deepEqual(after.pages.map((p) => p.nextCursor), ['c0', null]);
  assert.deepEqual(ids(before), [['a', 'b'], ['c', 'd']], 'the cached data is not mutated');
});

test('a changed chat moves to the top instead of appearing twice', () => {
  const after = m.applyChatChange(pages(['a', 'b'], ['c', 'd']), chat('d', 'renamed'), ALL);
  assert.deepEqual(ids(after), [['d', 'a', 'b'], ['c']]);
  assert.equal(after.pages[0].chats[0].title, 'renamed');
});

test('archiving removes the chat from lists without archived chats, moves it in lists with them', () => {
  const archived = chat('c', 'chat c', true);
  assert.deepEqual(ids(m.applyChatChange(pages(['a', 'b'], ['c']), archived, ALL)), [['a', 'b'], []]);
  assert.deepEqual(
    ids(m.applyChatChange(pages(['a', 'b'], ['c']), archived, { includeArchived: true, q: '' })),
    [['c', 'a', 'b'], []],
  );
});

test('a searched list only takes chats whose title matches, case-insensitively', () => {
  const searched = { includeArchived: false, q: 'Report' };
  assert.deepEqual(ids(m.applyChatChange(pages(['a']), chat('n', 'Q3 report'), searched)), [['n', 'a']]);
  assert.deepEqual(ids(m.applyChatChange(pages(['a']), chat('n', 'other'), searched)), [['a']]);
});

test('in-flight fetches: none, a next page, or the pages themselves', () => {
  assert.equal(m.inFlightFetch({ fetchStatus: 'idle', fetchMeta: null }), 'none');
  // A paused query is not fetching either: nothing will arrive to overwrite the edit.
  assert.equal(m.inFlightFetch({ fetchStatus: 'paused', fetchMeta: { fetchMore: { direction: 'forward' } } }), 'none');
  assert.equal(m.inFlightFetch({ fetchStatus: 'fetching', fetchMeta: { fetchMore: { direction: 'forward' } } }), 'next-page');
  assert.equal(m.inFlightFetch({ fetchStatus: 'fetching', fetchMeta: null }), 'pages');
  assert.equal(m.inFlightFetch({ fetchStatus: 'fetching' }), 'pages');
  assert.equal(m.inFlightFetch({ fetchStatus: 'fetching', fetchMeta: { fetchMore: { direction: 'backward' } } }), 'pages');
});

test('nothing cached: nothing to update', () => {
  assert.equal(m.applyChatChange(undefined, chat('n'), ALL), undefined);
  const empty = { pages: [], pageParams: [] };
  assert.equal(m.applyChatChange(empty, chat('n'), ALL), empty);
});
