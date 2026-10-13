/**
 * PipelineStepsEditor — ergonomic editor for pipeline step sequences.
 *
 * PORTABILITY CONTRACT (shared with model.ts): prop-driven and app-agnostic.
 * No API clients, no router, no app types — resource suggestions come in via
 * props; edited steps go out via onChange. Styling is semantic `pse-*` class
 * names only; each app ships its own stylesheet (console: steps-editor.css).
 * Dependencies: react, lucide-react.
 *
 * Ergonomics:
 * - one card per step with type-specific fields and resource suggestions;
 * - mapping fields with a per-row literal ⇄ JMESPath toggle (`.$` handled
 *   automatically), whole-input expression mode, and a lossless raw-JSON
 *   fallback per step (unknown keys are preserved, never dropped);
 * - switching Fields ⇄ Expression converts (a fields input is a JMESPath
 *   `{key: expr}` hash) and warns before a switch that can't keep the value;
 * - expressions are free text with a picker of the references available at
 *   that position (run input fields, earlier step outputs, cursor, run);
 * - cursor / retry sections where the backend allows them.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowDown, ArrowUp, Braces, ChevronDown, ChevronRight, Copy, Plus,
  SigmaSquare, Trash2, Type as TypeIcon,
} from 'lucide-react';
import type { InputMode, MappingRow, Step, StepType, SuggestionGroup } from './model';
import {
  STEP_TYPES, deriveInputMode, duplicateRowKeys, duplicateStep, expressionSuggestions,
  expressionToInput, extraKeys, inputSchemaFields, isEmptyInput, isPlainObject, inputToExpression, inputToRows,
  literalTypeHint, moveStep, newStep, removeStep, replaceStep, rowsToInput, setStepKey,
  stashFate, stepSummary, updateStep, validateSteps,
} from './model';
import './steps-editor.css';

export interface ConnectorResource {
  ref: string; // namespace/name
  operations: string[];
}

export interface StepResources {
  connectors?: ConnectorResource[];
  functions?: string[];
  agents?: string[];
  queries?: string[];
  connections?: string[]; // database connection names
}

export interface PipelineStepsEditorProps {
  steps: Step[];
  onChange: (steps: Step[]) => void;
  resources?: StepResources;
  /** The pipeline's inputSchema: its properties are offered as `input.<name>`. */
  inputSchema?: Record<string, any>;
}

const CUSTOM_SENTINEL = '\u0000custom';

/** Resource picker: a real dropdown when we know the options (datalist was
 * invisible-until-you-type and unreliable in Safari — users thought they had
 * to hand-type refs), with a Custom… escape and free text as the fallback,
 * so missing resources never block editing (install order, cross-package
 * refs). A current value that is not in the catalog stays selectable. */
function SuggestInput({
  value, onChange, options, placeholder, mono = true, invalid = false,
}: {
  value: string;
  onChange: (v: string) => void;
  options?: string[];
  placeholder?: string;
  mono?: boolean;
  invalid?: boolean;
}) {
  const [customMode, setCustomMode] = useState(false);
  const cls = `pse-input${mono ? ' pse-mono' : ''}${invalid ? ' pse-invalid' : ''}`;

  if (!options?.length || customMode) {
    return (
      <input
        className={cls}
        value={value}
        placeholder={placeholder}
        autoFocus={customMode}
        onChange={(e) => onChange(e.target.value)}
        onBlur={() => {
          // Re-offer the dropdown once the custom value matches a known ref
          if (options?.includes(value)) setCustomMode(false);
        }}
      />
    );
  }

  const known = options.includes(value);
  return (
    <select
      className={cls}
      value={known || value === '' ? value : value}
      onChange={(e) => {
        if (e.target.value === CUSTOM_SENTINEL) {
          setCustomMode(true);
          return;
        }
        onChange(e.target.value);
      }}
    >
      <option value="">{placeholder || 'Select…'}</option>
      {!known && value !== '' && <option value={value}>{value} (not found)</option>}
      {options.map((o) => (
        <option key={o} value={o}>{o}</option>
      ))}
      <option value={CUSTOM_SENTINEL}>Custom…</option>
    </select>
  );
}

function Field({ label, children, hint }: { label: string; children: React.ReactNode; hint?: string }) {
  return (
    <label className="pse-field">
      <span className="pse-label">{label}</span>
      {children}
      {hint && <span className="pse-hint">{hint}</span>}
    </label>
  );
}

/** A JMESPath expression: free text (any expression is valid here, not just
 * the listed references) plus a picker that inserts a reference at the
 * caret, or replaces the value when the field hasn't been focused yet. */
function ExpressionInput({
  value, onChange, suggestions, placeholder, multiline = false, invalid = false,
}: {
  value: string;
  onChange: (v: string) => void;
  suggestions: SuggestionGroup[];
  placeholder?: string;
  multiline?: boolean;
  invalid?: boolean;
}) {
  const ref = useRef<HTMLInputElement & HTMLTextAreaElement>(null);
  const caret = useRef<[number, number] | null>(null);
  const remember = () => {
    const el = ref.current;
    if (el) caret.current = [el.selectionStart ?? value.length, el.selectionEnd ?? value.length];
  };
  const insert = (text: string) => {
    const [start, end] = caret.current ?? [0, value.length];
    const next = value.slice(0, start) + text + value.slice(end);
    onChange(next);
    const pos = start + text.length;
    caret.current = [pos, pos];
    requestAnimationFrame(() => {
      ref.current?.focus();
      ref.current?.setSelectionRange(pos, pos);
    });
  };
  const cls = `pse-input pse-mono${invalid ? ' pse-invalid' : ''}`;
  const common = {
    ref,
    className: cls,
    value,
    placeholder,
    spellCheck: false,
    onSelect: remember,
    onKeyUp: remember,
    onClick: remember,
  };
  return (
    <div className="pse-expr">
      {multiline ? (
        <textarea {...common} rows={Math.min(6, Math.max(2, Math.ceil(value.length / 70)))}
          onChange={(e) => onChange(e.target.value)} />
      ) : (
        <input {...common} onChange={(e) => onChange(e.target.value)} />
      )}
      <select
        className="pse-input pse-expr-pick"
        value=""
        title="Insert a reference available at this step"
        onChange={(e) => e.target.value && insert(e.target.value)}
      >
        <option value="">Insert…</option>
        {suggestions.map((g) => (
          <optgroup key={g.label} label={g.label}>
            {g.options.map((o) => (
              <option key={o} value={o}>{o}</option>
            ))}
          </optgroup>
        ))}
      </select>
    </div>
  );
}

/** Per-row mapping editor: key, literal/expression toggle, value. Rows live
 * in local state so a row whose key is still empty (just added, or being
 * retyped) stays on screen; only keyed rows are written to the step. */
function MappingRowsEditor({
  input, onInput, suggestions,
}: {
  input: Record<string, any> | undefined;
  onInput: (input: Record<string, any>) => void;
  suggestions: SuggestionGroup[];
}) {
  const [rows, setRows] = useState<MappingRow[]>(() => inputToRows(input));
  const incoming = JSON.stringify(input ?? {});
  const emitted = useRef(incoming);
  useEffect(() => {
    // Changed from outside (raw JSON edit, mode switch, reorder): re-derive.
    if (incoming !== emitted.current) {
      emitted.current = incoming;
      setRows(inputToRows(JSON.parse(incoming)));
    }
  }, [incoming]);

  const commit = (next: MappingRow[]) => {
    setRows(next);
    const value = rowsToInput(next);
    emitted.current = JSON.stringify(value);
    onInput(value);
  };
  const setRow = (i: number, patch: Partial<MappingRow>) => {
    // Typing `name.$` as a key means "expression": model it as the toggle.
    if (patch.key?.endsWith('.$')) patch = { ...patch, key: patch.key.slice(0, -2), kind: 'expr' };
    commit(rows.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));
  };
  const dups = duplicateRowKeys(rows);

  return (
    <div className="pse-mapping">
      {rows.length === 0 && <span className="pse-hint">No fields — the step gets an empty object.</span>}
      {rows.map((row, i) => {
        const hint = row.kind === 'literal' ? literalTypeHint(row.value) : null;
        return (
          <div key={i} className="pse-mapping-row">
            <input
              className={`pse-input pse-mono pse-mapping-key${dups.includes(row.key.trim()) ? ' pse-invalid' : ''}`}
              value={row.key}
              placeholder="param"
              autoFocus={row.key === '' && row.value === '' && i === rows.length - 1}
              onChange={(e) => setRow(i, { key: e.target.value })}
            />
            <button
              type="button"
              className={`pse-kind-toggle${row.kind === 'expr' ? ' pse-kind-expr' : ''}`}
              title={row.kind === 'expr'
                ? 'JMESPath expression (click for a fixed value)'
                : 'Fixed value (click for a JMESPath expression)'}
              onClick={() => setRow(i, { kind: row.kind === 'expr' ? 'literal' : 'expr' })}
            >
              {row.kind === 'expr' ? <SigmaSquare size={13} /> : <TypeIcon size={13} />}
            </button>
            <div className="pse-mapping-value">
              {row.kind === 'expr' ? (
                <ExpressionInput
                  value={row.value}
                  onChange={(v) => setRow(i, { value: v })}
                  suggestions={suggestions}
                  placeholder="steps.fetch.output.body"
                />
              ) : (
                <input
                  className="pse-input pse-mono"
                  value={row.value}
                  placeholder='value — 42, true, {"a": 1} are typed; =42 is the string "42"'
                  onChange={(e) => setRow(i, { value: e.target.value })}
                />
              )}
              {hint && <span className="pse-type-badge">{hint}</span>}
            </div>
            <button
              type="button"
              className="pse-icon-btn"
              title="Remove"
              onClick={() => commit(rows.filter((_, idx) => idx !== i))}
            >
              <Trash2 size={13} />
            </button>
          </div>
        );
      })}
      {dups.length > 0 && (
        <span className="pse-error">Duplicate field {dups.map((d) => `'${d}'`).join(', ')} — the last one wins.</span>
      )}
      <button
        type="button"
        className="pse-add-row"
        onClick={() => setRows([...rows, { key: '', value: '', kind: 'literal' }])}
      >
        <Plus size={13} /> Add field
      </button>
    </div>
  );
}

/** What a step at `index` can read, in words, for the Input section. */
function availabilityHint(steps: Step[], index: number, inputSchema?: Record<string, any>): string {
  const fields = inputSchemaFields(inputSchema);
  const input = fields.length
    ? `the run input (${fields.slice(0, 4).map((f) => `input.${f.name}`).join(', ')}${fields.length > 4 ? ', …' : ''})`
    : 'the run input (input — declare its fields under Run input)';
  if (index === 0) {
    return `First step: no earlier step outputs yet. It can use fixed values, ${input}, cursor and run.`;
  }
  const prior = steps.slice(0, index).map((s) => `steps.${s.name}.output`);
  return `Can use fixed values, ${input}, ${prior.join(', ')}, cursor and run.`;
}

interface PendingSwitch {
  to: InputMode;
  message: string;
}

/** The step `input` editor with its three modes. */
function InputEditor({
  step, index, steps, onSteps, inputSchema,
}: {
  step: Step;
  index: number;
  steps: Step[];
  onSteps: (steps: Step[]) => void;
  inputSchema?: Record<string, any>;
}) {
  // The mode is DERIVED from the step's actual shape (reorder/raw-edit safe);
  // the only sticky UI state is "show me the JSON anyway".
  const naturalMode = deriveInputMode(step);
  const [jsonView, setJsonView] = useState(false);
  const [jsonText, setJsonText] = useState('');
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [pending, setPending] = useState<PendingSwitch | null>(null);
  // A switch that can't convert keeps what it replaced here (the spec forbids
  // carrying both `input` and `input.$`), so switching back restores it.
  const [stash, setStash] = useState<{ step: string; input?: any; expr?: string } | null>(null);
  const mode: InputMode = jsonView || naturalMode === 'json' ? 'json' : naturalMode;
  const suggestions = expressionSuggestions(steps, index, inputSchema);
  const hasExpr = 'input.$' in step;
  const mine = stash?.step === step.name ? stash : null;

  // Keep the stash tied to its step across reorders, renames and deletes.
  const prevCount = useRef(steps.length);
  useEffect(() => {
    const countChanged = prevCount.current !== steps.length;
    prevCount.current = steps.length;
    if (!stash) return;
    const fate = stashFate(stash.step, step.name, steps.map((s) => s.name), countChanged);
    if (fate === 'carry') setStash({ ...stash, step: step.name });
    else if (fate === 'clear') setStash(null);
  }, [step.name, steps, stash]);

  const jsonOf = (value: { input?: any; expr?: string }) =>
    JSON.stringify(value.expr !== undefined ? { 'input.$': value.expr } : value.input ?? {}, null, 2);
  const currentJson = () => jsonOf(hasExpr ? { expr: step['input.$'] } : { input: step.input });

  const write = (value: { input?: any; expr?: string }) => {
    let s = setStepKey(steps, index, 'input', value.input);
    s = setStepKey(s, index, 'input.$', value.expr);
    onSteps(s);
  };

  const switchMode = (next: InputMode, confirmed = false) => {
    setPending(null);
    setJsonError(null);
    // Still-empty fields after a switch that couldn't convert: bring back
    // what that switch replaced.
    const restorable = !hasExpr && isEmptyInput(step.input) && mine;

    if (next === 'json') {
      if (restorable) {
        const value = mine.input !== undefined ? { input: mine.input } : { expr: mine.expr };
        write(value);
        setJsonText(jsonOf(value));
      } else {
        setJsonText(currentJson());
      }
      setJsonView(true);
      return;
    }

    if (next === 'expression') {
      setJsonView(false);
      if (hasExpr) return;
      // Always lossless: a fields input is a JMESPath {key: expr} hash.
      write({
        expr: restorable
          ? mine.expr ?? inputToExpression(mine.input)
          : inputToExpression(step.input),
      });
      return;
    }

    // next === 'fields'
    if (hasExpr) {
      const expr: string = step['input.$'] ?? '';
      const converted = expressionToInput(expr);
      if (converted) {
        setJsonView(false);
        write({ input: Object.keys(converted).length || !mine ? converted : restoreFields(mine) });
        return;
      }
      if (!confirmed) {
        setPending({
          to: 'fields',
          message: "This expression isn't a {key: value} object, so it can't be split into fields. "
            + 'Switching gives you empty fields; the expression is kept here and comes back if you '
            + 'switch to Expression again before leaving the page.',
        });
        return;
      }
      setStash({ step: step.name, expr });
      setJsonView(false);
      write({ input: mine ? restoreFields(mine) : {} });
      return;
    }
    if (step.input !== undefined && step.input !== null && !isPlainObject(step.input)) {
      if (!confirmed) {
        setPending({
          to: 'fields',
          message: `The input is a JSON ${Array.isArray(step.input) ? 'array' : 'value'}, not an object, `
            + 'so it has no fields. Switching gives you empty fields; the current value comes back '
            + 'if you switch to Expression or JSON again before leaving the page.',
        });
        return;
      }
      setStash({ step: step.name, input: step.input });
      write({ input: {} });
    }
    setJsonView(false);
  };

  const applyJson = (text: string) => {
    setJsonText(text);
    try {
      const parsed = JSON.parse(text);
      setJsonError(null);
      if (isPlainObject(parsed) && 'input.$' in parsed) write({ expr: parsed['input.$'] });
      else write({ input: parsed });
    } catch (e: any) {
      setJsonError(e.message);
    }
  };

  return (
    <div className="pse-section">
      <div className="pse-section-head">
        <span className="pse-section-title">Input</span>
        <div className="pse-mode-tabs">
          {(['fields', 'expression', 'json'] as const).map((m) => (
            <button
              key={m}
              type="button"
              className={`pse-mode-tab${mode === m ? ' pse-active' : ''}`}
              title={m === 'fields'
                ? 'One row per input key: a fixed value or an expression each'
                : m === 'expression'
                  ? 'The whole input as one JMESPath expression'
                  : 'The raw input JSON'}
              onClick={() => switchMode(m)}
            >
              {m === 'fields' ? 'Fields' : m === 'expression' ? 'Expression' : 'JSON'}
            </button>
          ))}
        </div>
      </div>
      <span className="pse-hint">{availabilityHint(steps, index, inputSchema)}</span>

      {pending && (
        <div className="pse-notice">
          <span>{pending.message}</span>
          <div className="pse-notice-actions">
            <button type="button" className="pse-notice-btn pse-notice-primary"
              onClick={() => switchMode(pending.to, true)}>
              Switch anyway
            </button>
            <button type="button" className="pse-notice-btn" onClick={() => setPending(null)}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {mode === 'fields' && (
        <MappingRowsEditor
          input={step.input}
          onInput={(input) => onSteps(setStepKey(steps, index, 'input', input))}
          suggestions={suggestions}
        />
      )}

      {mode === 'expression' && (
        <Field
          label="Whole input as one JMESPath expression"
          hint="An object expression like {key: steps.fetch.output.id, other: input.x} can also be edited as Fields."
        >
          <ExpressionInput
            value={step['input.$'] ?? ''}
            onChange={(v) => onSteps(setStepKey(steps, index, 'input.$', v))}
            suggestions={suggestions}
            placeholder="steps.fetch.output.body"
            multiline
            invalid={!String(step['input.$'] ?? '').trim()}
          />
        </Field>
      )}

      {mode === 'json' && (
        <div className="pse-field">
          <textarea
            className={`pse-input pse-mono pse-json${jsonError ? ' pse-invalid' : ''}`}
            rows={6}
            value={jsonView ? jsonText : currentJson()}
            onChange={(e) => {
              if (!jsonView) setJsonView(true);
              applyJson(e.target.value);
            }}
            spellCheck={false}
          />
          {jsonError && <span className="pse-error">{jsonError}</span>}
          <span className="pse-hint">
            Keys ending in `.$` are JMESPath, at any depth. {'{"input.$": "…"}'} sets the whole input as one expression.
          </span>
        </div>
      )}
    </div>
  );
}

/** Fields to restore from a stash: its object input, or its expression when
 * that converts. */
function restoreFields(stash: { input?: any; expr?: string }): Record<string, any> {
  if (isPlainObject(stash.input)) return stash.input;
  return (stash.expr !== undefined && expressionToInput(stash.expr)) || {};
}

function CursorEditor({
  step, index, steps, onSteps,
}: {
  step: Step;
  index: number;
  steps: Step[];
  onSteps: (steps: Step[]) => void;
}) {
  const cursor = step.cursor;
  const otherHasCursor = steps.some((s, i) => i !== index && s.cursor);
  return (
    <div className="pse-section">
      <div className="pse-section-head">
        <label className="pse-check">
          <input
            type="checkbox"
            checked={!!cursor}
            disabled={!cursor && otherHasCursor}
            onChange={(e) =>
              onSteps(setStepKey(steps, index, 'cursor', e.target.checked ? { param: '', path: '' } : undefined))
            }
          />
          <span className="pse-section-title">Cursor</span>
        </label>
        {!cursor && otherHasCursor && <span className="pse-hint">another step already owns the cursor</span>}
      </div>
      {cursor && (
        <div className="pse-grid-3">
          <Field label="Inject as param">
            <input
              className="pse-input pse-mono"
              value={cursor.param ?? ''}
              placeholder="startHistoryId"
              onChange={(e) => onSteps(setStepKey(steps, index, 'cursor', { ...cursor, param: e.target.value }))}
            />
          </Field>
          <Field label="Read new mark from (JMESPath)">
            <input
              className="pse-input pse-mono"
              value={cursor.path ?? ''}
              placeholder="body.historyId"
              onChange={(e) => onSteps(setStepKey(steps, index, 'cursor', { ...cursor, path: e.target.value }))}
            />
          </Field>
          <Field label="Initial value (optional)">
            <input
              className="pse-input pse-mono"
              value={cursor.initial ?? ''}
              placeholder="empty = omit param on first run"
              onChange={(e) => {
                const next = { ...cursor };
                if (e.target.value === '') delete next.initial;
                else next.initial = e.target.value;
                onSteps(setStepKey(steps, index, 'cursor', next));
              }}
            />
          </Field>
        </div>
      )}
    </div>
  );
}

function RetryEditor({
  step, index, steps, onSteps,
}: {
  step: Step;
  index: number;
  steps: Step[];
  onSteps: (steps: Step[]) => void;
}) {
  const retry = step.retry ?? {};
  const attempts = retry.maxAttempts ?? 1;
  return (
    <div className="pse-inline-fields">
      <Field label="Retry attempts">
        <input
          className="pse-input pse-narrow"
          type="number"
          min={1}
          max={10}
          value={attempts}
          onChange={(e) => {
            const v = parseInt(e.target.value || '1', 10);
            onSteps(setStepKey(steps, index, 'retry', v > 1 ? { ...retry, maxAttempts: v } : undefined));
          }}
        />
      </Field>
      {attempts > 1 && (
        <Field label="Backoff">
          <select
            className="pse-input"
            value={retry.backoff ?? 'none'}
            onChange={(e) => onSteps(setStepKey(steps, index, 'retry', { ...retry, backoff: e.target.value }))}
          >
            <option value="none">none</option>
            <option value="linear">linear</option>
            <option value="exponential">exponential</option>
          </select>
        </Field>
      )}
    </div>
  );
}

function StepBody({
  step, index, steps, onSteps, resources, inputSchema,
}: {
  step: Step;
  index: number;
  steps: Step[];
  onSteps: (steps: Step[]) => void;
  resources?: StepResources;
  inputSchema?: Record<string, any>;
}) {
  const suggestions = expressionSuggestions(steps, index, inputSchema);
  const connectorRefs = resources?.connectors?.map((c) => c.ref);
  const operations = useMemo(
    () => resources?.connectors?.find((c) => c.ref === step.connector)?.operations,
    [resources, step.connector]
  );

  return (
    <div className="pse-body">
      {step.type === 'connector' && (
        <>
          <div className="pse-grid-2">
            <Field label="Connector">
              <SuggestInput
                value={step.connector ?? ''}
                onChange={(v) => onSteps(updateStep(steps, index, { connector: v }))}
                options={connectorRefs}
                placeholder="google/gmail"
              />
            </Field>
            <Field label="Operation">
              <SuggestInput
                value={step.operation ?? ''}
                onChange={(v) => onSteps(updateStep(steps, index, { operation: v }))}
                options={operations}
                placeholder="list-history"
              />
            </Field>
          </div>
          <InputEditor step={step} index={index} steps={steps} onSteps={onSteps} inputSchema={inputSchema} />
          <CursorEditor step={step} index={index} steps={steps} onSteps={onSteps} />
          <div className="pse-inline-fields">
            <RetryEditor step={step} index={index} steps={steps} onSteps={onSteps} />
            <Field label="Also accept statuses" hint="comma-separated, e.g. 404">
              <input
                className="pse-input pse-narrow pse-mono"
                value={(step.allowStatuses ?? []).join(', ')}
                onChange={(e) => {
                  const statuses = e.target.value
                    .split(',')
                    .map((s) => parseInt(s.trim(), 10))
                    .filter((n) => !Number.isNaN(n));
                  onSteps(setStepKey(steps, index, 'allowStatuses', statuses.length ? statuses : undefined));
                }}
              />
            </Field>
          </div>
        </>
      )}

      {step.type === 'function' && (
        <>
          <Field label="Function">
            <SuggestInput
              value={step.function ?? ''}
              onChange={(v) => onSteps(updateStep(steps, index, { function: v }))}
              options={resources?.functions}
              placeholder="gmail/extract-messages"
            />
          </Field>
          <InputEditor step={step} index={index} steps={steps} onSteps={onSteps} inputSchema={inputSchema} />
          <CursorEditor step={step} index={index} steps={steps} onSteps={onSteps} />
          <RetryEditor step={step} index={index} steps={steps} onSteps={onSteps} />
        </>
      )}

      {step.type === 'agent' && (
        <>
          <Field label="Agent" hint="Structured output requires the agent to declare an outputSchema; the reply is validated against it. Agent steps are never auto-retried.">
            <SuggestInput
              value={step.agent ?? ''}
              onChange={(v) => onSteps(updateStep(steps, index, { agent: v }))}
              options={resources?.agents}
              placeholder="gmail/triage"
            />
          </Field>
          <InputEditor step={step} index={index} steps={steps} onSteps={onSteps} inputSchema={inputSchema} />
          <div className="pse-section">
            <div className="pse-section-head">
              <span className="pse-section-title">Message</span>
              <button
                type="button"
                className={`pse-kind-toggle${'message.$' in step ? ' pse-kind-expr' : ''}`}
                title={'message.$' in step ? 'JMESPath expression (click for literal)' : 'Literal text (click for JMESPath)'}
                onClick={() => {
                  if ('message.$' in step) {
                    let s = setStepKey(steps, index, 'message.$', undefined);
                    onSteps(setStepKey(s, index, 'message', step['message.$'] ?? ''));
                  } else {
                    let s = setStepKey(steps, index, 'message', undefined);
                    onSteps(setStepKey(s, index, 'message.$', step.message ?? ''));
                  }
                }}
              >
                {'message.$' in step ? <SigmaSquare size={13} /> : <TypeIcon size={13} />}
              </button>
            </div>
            {'message.$' in step ? (
              <ExpressionInput
                value={step['message.$'] ?? ''}
                onChange={(v) => onSteps(setStepKey(steps, index, 'message.$', v))}
                suggestions={suggestions}
                placeholder="steps.extract.output.summary"
              />
            ) : (
              <textarea
                className="pse-input"
                rows={2}
                value={step.message ?? ''}
                placeholder="Empty = the JSON-serialized input is sent as the message"
                onChange={(e) =>
                  onSteps(setStepKey(steps, index, 'message', e.target.value === '' ? undefined : e.target.value))
                }
              />
            )}
          </div>
        </>
      )}

      {step.type === 'query' && (
        <>
          <Field label="Query">
            <SuggestInput
              value={step.query ?? ''}
              onChange={(v) => onSteps(updateStep(steps, index, { query: v }))}
              options={resources?.queries}
              placeholder="search/similar-docs"
            />
          </Field>
          <InputEditor step={step} index={index} steps={steps} onSteps={onSteps} inputSchema={inputSchema} />
          <RetryEditor step={step} index={index} steps={steps} onSteps={onSteps} />
        </>
      )}

      {step.type === 'load' && (
        <>
          <div className="pse-grid-2">
            <Field label="Database connection">
              <SuggestInput
                value={step.connection ?? ''}
                onChange={(v) => onSteps(updateStep(steps, index, { connection: v }))}
                options={resources?.connections}
                placeholder="reporting"
              />
            </Field>
            <Field label="Table" hint="auto-created on first run (pk + jsonb payload)">
              <input
                className="pse-input pse-mono"
                value={step.table ?? ''}
                placeholder="jira_worklogs"
                onChange={(e) => onSteps(updateStep(steps, index, { table: e.target.value }))}
              />
            </Field>
          </div>
          <div className="pse-grid-2">
            <Field label="Items (JMESPath)" hint="array to upsert, one row per item">
              <ExpressionInput
                value={step['items.$'] ?? ''}
                onChange={(v) => onSteps(setStepKey(steps, index, 'items.$', v))}
                suggestions={suggestions}
                placeholder="steps.extract.output.worklogs"
              />
            </Field>
            <Field label="Primary key (JMESPath over each item)" hint="`item` is bound per item">
              <input
                className="pse-input pse-mono"
                value={step['primaryKey.$'] ?? ''}
                placeholder="item.id"
                onChange={(e) => onSteps(setStepKey(steps, index, 'primaryKey.$', e.target.value))}
              />
            </Field>
          </div>
          <RetryEditor step={step} index={index} steps={steps} onSteps={onSteps} />
        </>
      )}
    </div>
  );
}

function StepCard({
  step, index, steps, onSteps, resources, inputSchema, issues,
}: {
  step: Step;
  index: number;
  steps: Step[];
  onSteps: (steps: Step[]) => void;
  resources?: StepResources;
  inputSchema?: Record<string, any>;
  issues: string[];
}) {
  const [collapsed, setCollapsed] = useState(false);
  const [rawMode, setRawMode] = useState(false);
  const [rawText, setRawText] = useState('');
  const [rawError, setRawError] = useState<string | null>(null);
  const extras = extraKeys(step);

  const enterRaw = () => {
    setRawText(JSON.stringify(step, null, 2));
    setRawError(null);
    setRawMode(true);
  };

  const applyRaw = (text: string) => {
    setRawText(text);
    try {
      const parsed = JSON.parse(text);
      setRawError(null);
      onSteps(replaceStep(steps, index, parsed));
    } catch (e: any) {
      setRawError(e.message);
    }
  };

  return (
    <div className={`pse-step${issues.length ? ' pse-step-invalid' : ''}`}>
      <div className="pse-step-head">
        <button type="button" className="pse-icon-btn" onClick={() => setCollapsed(!collapsed)}>
          {collapsed ? <ChevronRight size={15} /> : <ChevronDown size={15} />}
        </button>
        <span className="pse-step-index">{index + 1}</span>
        <select
          className="pse-input pse-type-select"
          value={step.type}
          onChange={(e) => {
            // Type change rebuilds the step but keeps its name.
            const fresh = newStep(e.target.value as StepType, steps.filter((_, i) => i !== index).map((s) => s.name));
            fresh.name = step.name;
            onSteps(replaceStep(steps, index, fresh));
          }}
        >
          {STEP_TYPES.map((t) => (
            <option key={t.type} value={t.type}>{t.label}</option>
          ))}
        </select>
        <input
          className="pse-input pse-mono pse-step-name"
          value={step.name}
          onChange={(e) => onSteps(updateStep(steps, index, { name: e.target.value }))}
        />
        {collapsed && <span className="pse-step-summary">{stepSummary(step)}</span>}
        {extras.length > 0 && !rawMode && (
          <span className="pse-extra-badge" title={`Keys only editable in JSON view: ${extras.join(', ')}`}>
            +{extras.length} raw
          </span>
        )}
        <div className="pse-step-actions">
          <button
            type="button"
            className={`pse-icon-btn${rawMode ? ' pse-active' : ''}`}
            title={rawMode ? 'Back to form view' : 'Edit this step as JSON'}
            onClick={() => (rawMode ? setRawMode(false) : enterRaw())}
          >
            <Braces size={14} />
          </button>
          <button type="button" className="pse-icon-btn" title="Move up" disabled={index === 0}
            onClick={() => onSteps(moveStep(steps, index, -1))}>
            <ArrowUp size={14} />
          </button>
          <button type="button" className="pse-icon-btn" title="Move down" disabled={index === steps.length - 1}
            onClick={() => onSteps(moveStep(steps, index, 1))}>
            <ArrowDown size={14} />
          </button>
          <button type="button" className="pse-icon-btn" title="Duplicate"
            onClick={() => onSteps(duplicateStep(steps, index))}>
            <Copy size={14} />
          </button>
          <button type="button" className="pse-icon-btn pse-danger" title="Delete"
            onClick={() => onSteps(removeStep(steps, index))}>
            <Trash2 size={14} />
          </button>
        </div>
      </div>

      {issues.length > 0 && !collapsed && (
        <div className="pse-issues">
          {issues.map((m, i) => (
            <span key={i} className="pse-error">{m}</span>
          ))}
        </div>
      )}

      {!collapsed && (rawMode ? (
        <div className="pse-body">
          <textarea
            className={`pse-input pse-mono pse-json${rawError ? ' pse-invalid' : ''}`}
            rows={10}
            value={rawText}
            onChange={(e) => applyRaw(e.target.value)}
            spellCheck={false}
          />
          {rawError && <span className="pse-error">{rawError}</span>}
        </div>
      ) : (
        <StepBody step={step} index={index} steps={steps} onSteps={onSteps} resources={resources} inputSchema={inputSchema} />
      ))}
    </div>
  );
}

export function PipelineStepsEditor({ steps, onChange, resources, inputSchema }: PipelineStepsEditorProps) {
  const issues = validateSteps(steps);
  const [addOpen, setAddOpen] = useState(false);

  return (
    <div className="pse">
      {steps.map((step, index) => (
        <div key={index} className="pse-step-slot">
          {index > 0 && <div className="pse-connector-line" />}
          <StepCard
            step={step}
            index={index}
            steps={steps}
            onSteps={onChange}
            resources={resources}
            inputSchema={inputSchema}
            issues={issues.filter((i) => i.index === index).map((i) => i.message)}
          />
        </div>
      ))}

      <div className="pse-add">
        {steps.length > 0 && <div className="pse-connector-line" />}
        {addOpen ? (
          <div className="pse-add-menu">
            {STEP_TYPES.map((t) => (
              <button
                key={t.type}
                type="button"
                className="pse-add-option"
                onClick={() => {
                  onChange([...steps, newStep(t.type, steps.map((s) => s.name))]);
                  setAddOpen(false);
                }}
              >
                <span className="pse-add-label">{t.label}</span>
                <span className="pse-hint">{t.hint}</span>
              </button>
            ))}
            <button type="button" className="pse-add-option pse-hint" onClick={() => setAddOpen(false)}>
              Cancel
            </button>
          </div>
        ) : (
          <button type="button" className="pse-add-btn" onClick={() => setAddOpen(true)}>
            <Plus size={15} /> Add step
          </button>
        )}
      </div>
    </div>
  );
}
