// Paged chat lists (GET /chats with limit/cursor) and keeping their cached
// pages current after a create, archive or restore without refetching them.
// Pure functions, so they can be unit-tested (tests/chat-pages.test.mjs).

export interface ChatListFilter {
  includeArchived: boolean;
  q: string;
}

export interface ChatListPage<T> {
  chats: T[];
  /** Cursor for the next page; null on the last page. */
  nextCursor: string | null;
}

/** The fields of a chat these helpers read. */
interface ChatLike {
  id: string;
  title: string;
  archived: boolean;
}

/** react-query's InfiniteData shape. */
export interface ChatPages<T> {
  pages: ChatListPage<T>[];
  pageParams: unknown[];
}

/** Query parameters for one page; empty filters are left out. */
export function chatListParams(
  filter: Partial<ChatListFilter> & { limit?: number; cursor?: string | null },
): Record<string, string | number | boolean> {
  const params: Record<string, string | number | boolean> = {};
  if (filter.includeArchived) params.include_archived = true;
  const q = filter.q?.trim();
  if (q) params.q = q;
  if (filter.limit !== undefined) params.limit = filter.limit;
  if (filter.cursor) params.cursor = filter.cursor;
  return params;
}

/** Whether the list showing `filter` would contain this chat (as the server filters). */
export function chatMatches(chat: ChatLike, filter: ChatListFilter): boolean {
  if (chat.archived && !filter.includeArchived) return false;
  const q = filter.q.trim().toLowerCase();
  return !q || chat.title.toLowerCase().includes(q);
}

/**
 * Apply a chat that was just created or changed to the cached pages. The
 * server orders by last update, and a create, archive or restore updates
 * the chat, so it moves to the top of the first page, or leaves the list
 * when it no longer matches the filter. Later pages' cursors stay valid:
 * they point past chats on the server, not at positions in the cache.
 */
export function applyChatChange<T extends ChatLike>(
  data: ChatPages<T> | undefined,
  chat: T,
  filter: ChatListFilter,
): ChatPages<T> | undefined {
  if (!data || data.pages.length === 0) return data;
  const pages = data.pages.map((page) => ({ ...page, chats: page.chats.filter((c) => c.id !== chat.id) }));
  if (chatMatches(chat, filter)) {
    pages[0] = { ...pages[0], chats: [chat, ...pages[0].chats] };
  }
  return { ...data, pages };
}
