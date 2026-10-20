// Wildcard entries in an agent's enabled_* lists ("namespace/*", "*/*").
// Pure functions (tested in tests/wildcard-refs.test.mjs).
//
// The editor lists each resource with a checkbox; a wildcard entry matches
// no single row, so it is shown (and added, configured or removed) on its
// own. The rules mirror the backend's (resource_resolver.py): only a whole
// name is a wildcard; anything else with a "*" is refused when the agent is
// saved. Entries are edited by position: a list may hold two entries for one
// pattern (e.g. a connector pattern with different operations), and editing
// one must leave the other alone.

/** The reference of a string entry, or of a dict entry under `key`. */
export function entryRef(entry: unknown, key?: string): string {
  if (typeof entry === 'string') return entry;
  if (key && entry && typeof entry === 'object') {
    const value = (entry as Record<string, unknown>)[key];
    return typeof value === 'string' ? value : '';
  }
  return '';
}

// "namespace/*" or "*/*".
export function isWildcardRef(ref: string): boolean {
  if (ref === '*/*') return true;
  const slash = ref.indexOf('/');
  if (slash <= 0) return false;
  const namespace = ref.slice(0, slash);
  return ref.slice(slash + 1) === '*' && !namespace.includes('*');
}

/** Why a typed pattern can't be added, or null when it can. */
export function wildcardPatternProblem(ref: string): string | null {
  const trimmed = ref.trim();
  if (!trimmed) return 'Enter a pattern';
  if (isWildcardRef(trimmed)) return null;
  return "Use 'namespace/*' (everything in a namespace) or '*/*' (everything)";
}

/** The positions of the wildcard entries in a list, in list order. */
export function wildcardIndices(entries: unknown[] | null | undefined, key?: string): number[] {
  const indices: number[] = [];
  (entries || []).forEach((e, i) => {
    if (isWildcardRef(entryRef(e, key))) indices.push(i);
  });
  return indices;
}

/** Whether the list already has an entry for `ref`. */
export function hasRef(entries: unknown[] | null | undefined, ref: string, key?: string): boolean {
  return (entries || []).some((e) => entryRef(e, key) === ref);
}

/** The list without the entry at `index`. */
export function withoutAt<T>(entries: T[] | null | undefined, index: number): T[] {
  return (entries || []).filter((_, i) => i !== index);
}

/** The list with the entry at `index` replaced by `next`; the others untouched. */
export function withReplacedAt<T>(entries: T[] | null | undefined, index: number, next: T): T[] {
  return (entries || []).map((e, i) => (i === index ? next : e));
}

/** Comma-separated text to a list of names ("" = none). */
export function parseNameList(text: string): string[] {
  return text.split(',').map((t) => t.trim()).filter(Boolean);
}

/**
 * Text typed as a JSON object (a connector entry's parameters by operation),
 * or null when it isn't one. Empty text is an empty object.
 */
export function parseJsonObject(text: string): Record<string, unknown> | null {
  if (!text.trim()) return {};
  try {
    const value = JSON.parse(text);
    return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
  } catch {
    return null;
  }
}
