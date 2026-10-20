import { useState } from 'react';
import { entryRef, wildcardEntries, wildcardPatternProblem, withEntry, withoutRef } from '../lib/wildcardRefs';

type Access = 'readonly' | 'readwrite';

interface WildcardPatternsProps<T> {
  /** The enabled_* list being edited. */
  entries: T[] | null | undefined;
  /** The dict key holding the reference; omit for a list of strings. */
  refKey?: string;
  /** What the list holds, for the help text ("components", "stores"). */
  noun: string;
  /** Offer a read / read-write choice (stores, collections). */
  withAccess?: boolean;
  /** The entry to store for a new pattern. */
  makeEntry: (ref: string, access: Access) => T;
  /** A short summary of a dict entry's settings, shown next to it. */
  describe?: (entry: T) => string | undefined;
  onChange: (entries: T[]) => void;
}

// The wildcard entries ("namespace/*", "*/*") of an enabled_* list: they
// match no single row of the editor's resource list, so they are listed,
// added and removed here. A wildcard only reaches the resources the chat's
// user may use themselves; a resource named in the list below decides its
// own settings.
export function WildcardPatterns<T>({
  entries, refKey, noun, withAccess, makeEntry, describe, onChange,
}: WildcardPatternsProps<T>) {
  const [draft, setDraft] = useState('');
  const [access, setAccess] = useState<Access>('readonly');
  const [error, setError] = useState<string | null>(null);
  const patterns = wildcardEntries(entries, refKey);

  const add = () => {
    const problem = wildcardPatternProblem(draft);
    if (problem) {
      setError(problem);
      return;
    }
    onChange(withEntry(entries, makeEntry(draft.trim(), access), refKey));
    setDraft('');
    setError(null);
  };

  return (
    <div className="mb-3">
      <p className="text-xs text-gray-500 mb-2">
        Wildcard patterns (<code className="text-xs bg-surface-1 px-1 rounded">namespace/*</code>,{' '}
        <code className="text-xs bg-surface-1 px-1 rounded">*/*</code>) enable every matching {noun} the
        chat's user may use themselves.
      </p>
      {patterns.length > 0 && (
        <div className="flex flex-wrap gap-2 mb-2">
          {patterns.map((entry) => {
            const ref = entryRef(entry, refKey);
            const detail = describe?.(entry);
            return (
              <span
                key={ref}
                className="inline-flex items-center gap-2 px-2 py-1 rounded border border-line-soft bg-surface-1 text-xs"
              >
                <span className="font-mono text-gray-100">{ref}</span>
                {detail && <span className="text-gray-500">{detail}</span>}
                <button
                  type="button"
                  className="text-gray-500 hover:text-gray-200"
                  aria-label={`Remove ${ref}`}
                  onClick={() => onChange(withoutRef(entries, ref, refKey))}
                >
                  ×
                </button>
              </span>
            );
          })}
        </div>
      )}
      <div className="flex gap-2 items-center">
        <input
          type="text"
          value={draft}
          onChange={(e) => {
            setDraft(e.target.value);
            setError(null);
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              add();
            }
          }}
          placeholder="namespace/*"
          className="input flex-1 font-mono text-sm"
        />
        {withAccess && (
          <select
            value={access}
            onChange={(e) => setAccess(e.target.value as Access)}
            className="input text-sm w-auto"
          >
            <option value="readonly">read</option>
            <option value="readwrite">read/write</option>
          </select>
        )}
        <button type="button" className="btn btn-secondary text-xs" onClick={add}>
          Add pattern
        </button>
      </div>
      {error && <p className="text-xs text-red-400 mt-1">{error}</p>}
    </div>
  );
}
