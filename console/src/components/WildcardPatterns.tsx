import { useState, type ReactNode } from 'react';
import {
  entryRef,
  parseJsonObject,
  parseNameList,
  wildcardEntries,
  wildcardPatternProblem,
  withEntry,
  withoutRef,
  withReplaced,
} from '../lib/wildcardRefs';

type Access = 'readonly' | 'readwrite';

interface WildcardPatternsProps<T> {
  /** The enabled_* list being edited. */
  entries: T[] | null | undefined;
  /** The dict key holding the reference; omit for a list of strings. */
  refKey?: string;
  /** What the list holds, for the help text ("components", "stores"). */
  noun: string;
  /** Offer a read / read-write choice when adding (stores, collections). */
  withAccess?: boolean;
  /** The entry to store for a new pattern. */
  makeEntry: (ref: string, access: Access) => T;
  /** Controls for an entry's own settings; `change` stores the edited entry. */
  settings?: (entry: T, change: (next: T) => void) => ReactNode;
  onChange: (entries: T[]) => void;
}

// The wildcard entries ("namespace/*", "*/*") of an enabled_* list: they
// match no single row of the editor's resource list, so they are listed,
// added, configured and removed here. A wildcard only reaches the resources
// the chat's user may use themselves; a resource named in the list below
// decides its own settings.
export function WildcardPatterns<T>({
  entries, refKey, noun, withAccess, makeEntry, settings, onChange,
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
        chat's user may use themselves; a {noun.replace(/s$/, '')} enabled by name below keeps its own settings.
      </p>
      {patterns.length > 0 && (
        <div className="space-y-2 mb-2">
          {patterns.map((entry) => {
            const ref = entryRef(entry, refKey);
            return (
              <div key={ref} className="border border-line-soft rounded-lg p-2">
                <div className="flex items-center justify-between">
                  <span className="text-sm font-mono text-gray-100">{ref}</span>
                  <button
                    type="button"
                    className="text-xs text-gray-500 hover:text-gray-200"
                    aria-label={`Remove ${ref}`}
                    onClick={() => onChange(withoutRef(entries, ref, refKey))}
                  >
                    Remove
                  </button>
                </div>
                {settings && (
                  <div className="mt-2">
                    {settings(entry, (next) => onChange(withReplaced(entries, ref, next, refKey)))}
                  </div>
                )}
              </div>
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

/** A read / read-write choice for an entry with an access mode. */
export function AccessSetting({ value, onChange }: { value: string | undefined; onChange: (access: Access) => void }) {
  return (
    <select
      value={value === 'readwrite' ? 'readwrite' : 'readonly'}
      onChange={(e) => onChange(e.target.value as Access)}
      className="input text-xs w-auto"
    >
      <option value="readonly">read</option>
      <option value="readwrite">read/write (where the user may write)</option>
    </select>
  );
}

/** Comma-separated names (operations, tool globs), stored on blur; empty = all. */
export function NameListSetting({
  label, value, placeholder, onChange,
}: { label: string; value: string[] | undefined; placeholder: string; onChange: (names: string[]) => void }) {
  const [draft, setDraft] = useState<string | null>(null);
  return (
    <label className="block">
      <span className="text-xs text-gray-500">{label}</span>
      <input
        type="text"
        value={draft ?? (value || []).join(', ')}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => {
          if (draft !== null) onChange(parseNameList(draft));
          setDraft(null);
        }}
        placeholder={placeholder}
        className="input w-full font-mono text-sm mt-1"
      />
    </label>
  );
}

/** A JSON object (a connector entry's locked parameters by operation), stored on blur when valid. */
export function JsonObjectSetting({
  label, value, onChange,
}: { label: string; value: Record<string, unknown> | undefined; onChange: (next: Record<string, unknown>) => void }) {
  const shown = value && Object.keys(value).length > 0 ? JSON.stringify(value, null, 2) : '';
  const [draft, setDraft] = useState<string | null>(null);
  const invalid = draft !== null && parseJsonObject(draft) === null;
  return (
    <label className="block">
      <span className="text-xs text-gray-500">{label}</span>
      <textarea
        value={draft ?? shown}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => {
          const parsed = draft === null ? null : parseJsonObject(draft);
          if (parsed !== null) {
            onChange(parsed);
            setDraft(null);
          }
        }}
        placeholder='{"post_message": {"channel": "{{ channel }}"}}'
        rows={3}
        className="input w-full font-mono text-xs mt-1"
      />
      {invalid && <span className="text-xs text-red-400">Not a JSON object: not saved</span>}
    </label>
  );
}
