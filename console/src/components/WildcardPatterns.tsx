import { useState, type ReactNode } from 'react';
import {
  entryRef,
  hasRef,
  parseJsonObject,
  parseNameList,
  wildcardIndices,
  wildcardPatternProblem,
  withoutAt,
  withReplacedAt,
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
  const list = entries || [];
  const positions = wildcardIndices(list, refKey);

  const add = () => {
    const ref = draft.trim();
    const problem = wildcardPatternProblem(ref) ?? (hasRef(list, ref, refKey) ? 'Already listed: edit it above' : null);
    if (problem) {
      setError(problem);
      return;
    }
    onChange([...list, makeEntry(ref, access)]);
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
      {positions.length > 0 && (
        <div className="space-y-2 mb-2">
          {positions.map((index) => {
            const entry = list[index];
            const ref = entryRef(entry, refKey);
            return (
              // By position: a list may hold two entries for one pattern.
              <div key={`${index}:${ref}`} className="border border-line-soft rounded-lg p-2">
                <div className="flex items-center justify-between">
                  <span className="text-sm font-mono text-gray-100">{ref}</span>
                  <button
                    type="button"
                    className="text-xs text-gray-500 hover:text-gray-200"
                    aria-label={`Remove ${ref}`}
                    onClick={() => onChange(withoutAt(list, index))}
                  >
                    Remove
                  </button>
                </div>
                {settings && (
                  <div className="mt-2">
                    {settings(entry, (next) => onChange(withReplacedAt(list, index, next)))}
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

/**
 * Comma-separated names (operations, tool globs); empty = all. Stored on
 * every keystroke, so a save (Enter included) never sends the old list; the
 * typed text is kept as is while the field has focus.
 */
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
        onChange={(e) => {
          setDraft(e.target.value);
          onChange(parseNameList(e.target.value));
        }}
        onBlur={() => setDraft(null)}
        placeholder={placeholder}
        className="input w-full font-mono text-sm mt-1"
      />
    </label>
  );
}

/**
 * A JSON object (a connector entry's locked parameters by operation), stored
 * whenever the text is valid JSON; while it isn't, the last valid value stays
 * and the field says so.
 */
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
        onChange={(e) => {
          setDraft(e.target.value);
          const parsed = parseJsonObject(e.target.value);
          if (parsed !== null) onChange(parsed);
        }}
        onBlur={() => {
          if (draft !== null && parseJsonObject(draft) !== null) setDraft(null);
        }}
        placeholder='{"post_message": {"channel": "{{ channel }}"}}'
        rows={3}
        className="input w-full font-mono text-xs mt-1"
      />
      {invalid && <span className="text-xs text-red-400">Not a JSON object yet: saving keeps the last valid parameters</span>}
    </label>
  );
}
