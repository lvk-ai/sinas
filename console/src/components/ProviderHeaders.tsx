import { useEffect, useState } from 'react';
import { Plus, Trash2 } from 'lucide-react';
import {
  HEADER_TEMPLATE_VARIABLES,
  configWithHeaders,
  headerRowProblems,
  headerRowsFromConfig,
  type HeaderRow,
} from '../lib/attribution';

/**
 * An LLM provider's identity headers (config.headers): name + template rows,
 * kept in step with the configuration JSON next to it (an edit there shows
 * here, and an edit here rewrites `headers` there). `disabled` while that
 * JSON doesn't parse, so an edit here can't overwrite an unfinished draft.
 * `onProblemChange` reports the rows' first problem (duplicates included,
 * which the config object can't hold), for the form to refuse saving.
 */
export function ProviderHeaders({
  idPrefix,
  config,
  onChange,
  disabled = false,
  onProblemChange,
}: {
  idPrefix: string;
  config: Record<string, unknown> | undefined;
  onChange: (config: Record<string, unknown>) => void;
  disabled?: boolean;
  onProblemChange?: (problem: string | null) => void;
}) {
  const [rows, setRows] = useState<HeaderRow[]>(() => headerRowsFromConfig(config));
  const configHeaders = JSON.stringify(config?.headers ?? {});

  // The JSON was edited by hand: show its headers.
  useEffect(() => {
    const ours = JSON.stringify(configWithHeaders({}, rows).headers ?? {});
    if (ours !== configHeaders) setRows(headerRowsFromConfig(config));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [configHeaders]);

  const update = (next: HeaderRow[]) => {
    setRows(next);
    onChange(configWithHeaders(config, next));
  };
  const problems = headerRowProblems(rows);
  const firstProblem = problems.find((p) => p !== null) ?? null;
  useEffect(() => {
    onProblemChange?.(firstProblem);
  }, [firstProblem, onProblemChange]);

  return (
    <div className="border-t border-line-soft pt-4">
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-sm font-medium text-gray-300">Headers</h3>
        <button
          type="button"
          onClick={() => update([...rows, { name: '', template: '' }])}
          disabled={disabled}
          className="btn btn-secondary text-xs flex items-center"
        >
          <Plus className="w-3 h-3 mr-1" />
          Add header
        </button>
      </div>
      <p className="text-xs text-gray-500 mb-2">
        Sent on every call through this provider, filled in from the call's identity, for a gateway to attribute
        and budget it. A header whose variables have no value is left out.{' '}
        <a
          href="https://docs.sinas.co/admin/llm-gateway-attribution"
          target="_blank"
          rel="noreferrer"
          className="text-primary-400 hover:underline"
        >
          How it works
        </a>
      </p>
      {disabled && (
        <p className="text-xs text-amber-400 mb-2">Fix the configuration JSON to edit headers here.</p>
      )}
      <div className="space-y-2">
        {rows.map((row, i) => (
          <div key={i}>
            <div className="flex items-center gap-2">
              <div className="w-48 flex-shrink-0">
                <input
                  id={`${idPrefix}-header-name-${i}`}
                  value={row.name}
                  onChange={(e) => update(rows.map((r, j) => (j === i ? { ...r, name: e.target.value } : r)))}
                  placeholder="x-customer-id"
                  disabled={disabled}
                  className="input font-mono text-xs"
                />
              </div>
              <div className="flex-1 min-w-0">
                <input
                  value={row.template}
                  onChange={(e) => update(rows.map((r, j) => (j === i ? { ...r, template: e.target.value } : r)))}
                  placeholder="{{attribution.orgId}}"
                  disabled={disabled}
                  className="input font-mono text-xs"
                />
              </div>
              <button
                type="button"
                onClick={() => update(rows.filter((_, j) => j !== i))}
                disabled={disabled}
                className="p-2 text-gray-500 hover:text-red-600"
                title="Remove header"
              >
                <Trash2 className="w-4 h-4" />
              </button>
            </div>
            {problems[i] && <p className="text-xs text-red-400 mt-1">{problems[i]}</p>}
          </div>
        ))}
      </div>
      <details className="mt-2 text-xs text-gray-500">
        <summary className="cursor-pointer">Variables</summary>
        <ul className="mt-1 space-y-0.5">
          {HEADER_TEMPLATE_VARIABLES.map((v) => (
            <li key={v.name}>
              <code className="text-gray-300">{v.name}</code>: {v.help}
            </li>
          ))}
        </ul>
        <p className="mt-1">
          Static text around them is kept. Credentials, trace context and SDK headers can't be set here; put a
          gateway key in the API key field.
        </p>
      </details>
    </div>
  );
}
