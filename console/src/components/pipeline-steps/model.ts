/**
 * Pipeline step editing model — pure logic, no React, no app imports.
 *
 * PORTABILITY CONTRACT: this module (and PipelineStepsEditor.tsx next to it)
 * are designed to be lifted verbatim into other Sinas apps (Studio, @sinas/ui).
 * They depend only on the step JSON shape defined by the backend
 * (backend/app/services/pipeline_validation.py) — keep it that way.
 *
 * Step shape reminders:
 * - keys ending in `.$` hold JMESPath expressions over
 *   {input, steps.<name>.output, cursor, run};
 * - a step's `input` is either a literal template object (whose own keys may
 *   use `.$`) or a whole-input expression under `input.$`;
 * - unknown keys must be preserved round-trip (the backend validates; the
 *   editor never silently drops what it doesn't model).
 */

export type StepType = 'connector' | 'function' | 'agent' | 'query' | 'load';

export type Step = { name: string; type: StepType } & Record<string, any>;

export interface StepTypeInfo {
  type: StepType;
  label: string;
  hint: string;
}

export const STEP_TYPES: StepTypeInfo[] = [
  { type: 'connector', label: 'Connector', hint: 'Call a connector operation (platform auth, retries)' },
  { type: 'function', label: 'Function', hint: 'Run a function in the function runtime' },
  { type: 'agent', label: 'Agent', hint: 'Invoke an agent; structured output when it has an outputSchema' },
  { type: 'query', label: 'Query', hint: 'Run a saved SQL query' },
  { type: 'load', label: 'Load', hint: 'Upsert items into a database table (idempotent sink)' },
];

/** Step-level keys the visual editor models per type; anything else is shown
 * as preserved "extra" keys and only editable in per-step JSON mode. */
const MODELED_KEYS: Record<StepType, string[]> = {
  connector: ['name', 'type', 'connector', 'operation', 'input', 'input.$', 'cursor', 'retry', 'allowStatuses'],
  function: ['name', 'type', 'function', 'input', 'input.$', 'cursor', 'retry'],
  agent: ['name', 'type', 'agent', 'input', 'input.$', 'message', 'message.$'],
  query: ['name', 'type', 'query', 'input', 'input.$', 'retry'],
  load: ['name', 'type', 'connection', 'table', 'primaryKey.$', 'items.$', 'retry'],
};

export const STEP_NAME_RE = /^[a-zA-Z_][a-zA-Z0-9_-]*$/;
export const REF_RE = /^[a-zA-Z_][a-zA-Z0-9_-]*\/[a-zA-Z_][a-zA-Z0-9_-]*$/;

export function extraKeys(step: Step): string[] {
  const modeled = MODELED_KEYS[step.type] ?? ['name', 'type'];
  return Object.keys(step).filter((k) => !modeled.includes(k));
}

// ---------------------------------------------------------------------------
// Step creation / list operations (all return new arrays/objects — no mutation)
// ---------------------------------------------------------------------------

export function uniqueStepName(base: string, existing: string[]): string {
  if (!existing.includes(base)) return base;
  let i = 2;
  while (existing.includes(`${base}${i}`)) i++;
  return `${base}${i}`;
}

export function newStep(type: StepType, existingNames: string[]): Step {
  const name = uniqueStepName(type === 'connector' ? 'fetch' : type, existingNames);
  switch (type) {
    case 'connector':
      return { name, type, connector: '', operation: '', input: {} };
    case 'function':
      return { name, type, function: '', input: {} };
    case 'agent':
      return { name, type, agent: '', input: {} };
    case 'query':
      return { name, type, query: '', input: {} };
    case 'load':
      return { name, type, connection: '', table: '', 'items.$': '', 'primaryKey.$': 'item.id' };
  }
}

export function moveStep(steps: Step[], index: number, delta: -1 | 1): Step[] {
  const target = index + delta;
  if (target < 0 || target >= steps.length) return steps;
  const next = [...steps];
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}

export function removeStep(steps: Step[], index: number): Step[] {
  return steps.filter((_, i) => i !== index);
}

export function duplicateStep(steps: Step[], index: number): Step[] {
  const copy: Step = JSON.parse(JSON.stringify(steps[index]));
  copy.name = uniqueStepName(copy.name, steps.map((s) => s.name));
  const next = [...steps];
  next.splice(index + 1, 0, copy);
  return next;
}

export function updateStep(steps: Step[], index: number, patch: Partial<Step>): Step[] {
  const next = [...steps];
  next[index] = { ...next[index], ...patch };
  return next;
}

/** Replace a step wholesale (used by per-step JSON mode). */
export function replaceStep(steps: Step[], index: number, step: Step): Step[] {
  const next = [...steps];
  next[index] = step;
  return next;
}

/** Set or delete a key on a step; deletes when value is undefined. */
export function setStepKey(steps: Step[], index: number, key: string, value: any): Step[] {
  const next = [...steps];
  const step: Step = { ...next[index] };
  if (value === undefined) delete step[key];
  else step[key] = value;
  next[index] = step;
  return next;
}

// ---------------------------------------------------------------------------
// Input mapping: fields ⇄ expression ⇄ raw JSON
// ---------------------------------------------------------------------------

export type InputMode = 'fields' | 'expression' | 'json';

export interface MappingRow {
  key: string;
  /** Displayed value: the expression text, or the literal (strings raw,
   * everything else JSON-encoded; see `literalText`). */
  value: string;
  kind: 'expr' | 'literal';
}

export function isPlainObject(v: any): v is Record<string, any> {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}

/** Which editing mode a step's current input supports without loss. Any
 * object is editable as fields (nested values show as JSON literals); only
 * a non-object `input` (array, scalar) needs the JSON view. */
export function deriveInputMode(step: Step): InputMode {
  if ('input.$' in step) return 'expression';
  const input = step.input;
  if (input === undefined || input === null) return 'fields';
  return isPlainObject(input) ? 'fields' : 'json';
}

/** Literal row values round-trip as JSON when they parse as JSON *and* aren't
 * plain words — `42`, `true`, `{"a":1}` become typed values; `hello` stays a
 * string. A leading `=` forces string (`=42` is the string "42"). */
export function parseLiteral(text: string): any {
  if (text.startsWith('=')) return text.slice(1);
  const trimmed = text.trim();
  if (trimmed === '') return '';
  if (/^(-?\d+(\.\d+)?([eE][+-]?\d+)?|true|false|null|[[{"].*)$/s.test(trimmed)) {
    try {
      return JSON.parse(trimmed);
    } catch {
      return text;
    }
  }
  return text;
}

/** Inverse of parseLiteral: the text that parses back to exactly `value`.
 * Strings that would otherwise read as another type (`"42"`, `"true"`) or
 * that start with `=` get the `=` escape, so editing never retypes a value. */
export function literalText(value: any): string {
  if (typeof value !== 'string') return JSON.stringify(value);
  return parseLiteral(value) === value ? value : `=${value}`;
}

export function inputToRows(input: Record<string, any> | undefined): MappingRow[] {
  if (!isPlainObject(input)) return [];
  return Object.entries(input).map(([key, value]) => {
    if (key.endsWith('.$')) {
      return { key: key.slice(0, -2), value: String(value), kind: 'expr' as const };
    }
    return { key, value: literalText(value), kind: 'literal' as const };
  });
}

export function literalTypeHint(text: string): string | null {
  const parsed = parseLiteral(text);
  if (typeof parsed === 'string') return text.startsWith('=') ? 'string' : null;
  if (parsed === null) return 'null';
  // Nested `.$` keys are still evaluated at run time, not passed through.
  if (hasTemplateKeys(parsed)) return 'template';
  if (Array.isArray(parsed)) return 'array';
  return typeof parsed; // number | boolean | object
}

/** Rows without a key are still being typed: they stay in the editor but
 * are not part of the input yet. On a duplicate key the last row wins.
 * Keys are written exactly as typed (whitespace only marks a blank row). */
export function rowsToInput(rows: MappingRow[]): Record<string, any> {
  const input: Record<string, any> = {};
  for (const row of rows) {
    const key = row.key;
    if (!key.trim()) continue;
    // A key can't be both literal and expression (the backend rejects it).
    delete input[key];
    delete input[`${key}.$`];
    if (row.kind === 'expr') input[`${key}.$`] = row.value;
    else input[key] = parseLiteral(row.value);
  }
  return input;
}

export function duplicateRowKeys(rows: MappingRow[]): string[] {
  const seen = new Set<string>();
  const dups = new Set<string>();
  for (const row of rows) {
    const key = row.key;
    if (!key.trim()) continue;
    if (seen.has(key)) dups.add(key);
    seen.add(key);
  }
  return [...dups];
}

// ---------------------------------------------------------------------------
// Fields ⇄ whole-input expression conversion
//
// A fields input `{a: 1, "b.$": "steps.x.output"}` and the expression
// `{a: \`1\`, b: steps.x.output}` (a JMESPath multiselect hash) resolve to the
// same value, so switching modes converts instead of discarding. Expressions
// that are not a top-level multiselect hash can't be split into fields.
// ---------------------------------------------------------------------------

const IDENT_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

function hasTemplateKeys(v: any): boolean {
  if (Array.isArray(v)) return v.some(hasTemplateKeys);
  if (isPlainObject(v)) return Object.entries(v).some(([k, x]) => k.endsWith('.$') || hasTemplateKeys(x));
  return false;
}

function jsonLiteral(v: any): string {
  return '`' + JSON.stringify(v).replace(/`/g, '\\`') + '`';
}

/** `top`: the input object itself always becomes a hash (so it splits back
 * into fields); nested values only when they hold `.$` keys. */
function templateToExpression(v: any, top = false): string {
  if (isPlainObject(v) && Object.keys(v).length && (top || hasTemplateKeys(v))) {
    const parts = Object.entries(v).map(([k, x]) => {
      const isExpr = k.endsWith('.$');
      const base = isExpr ? k.slice(0, -2) : k;
      const key = IDENT_RE.test(base) ? base : JSON.stringify(base);
      return `${key}: ${isExpr ? String(x) : templateToExpression(x)}`;
    });
    return `{${parts.join(', ')}}`;
  }
  if (Array.isArray(v) && hasTemplateKeys(v)) {
    return `[${v.map((x) => templateToExpression(x)).join(', ')}]`;
  }
  if (typeof v === 'string' && !/['\\]/.test(v)) return `'${v}'`;
  return jsonLiteral(v);
}

export const EMPTY_INPUT_EXPRESSION = '`{}`';

/** The whole-input expression equivalent to a fields input. An empty (or
 * absent) input is the literal `{}`: the backend rejects an empty expression. */
export function inputToExpression(input: any): string {
  if (input === undefined || input === null) return EMPTY_INPUT_EXPRESSION;
  return templateToExpression(input, true);
}

export function isEmptyInput(input: any): boolean {
  return input === undefined || input === null || (isPlainObject(input) && Object.keys(input).length === 0);
}

/** Split `text` on `sep` at nesting depth 0, skipping quoted/literal spans.
 * Returns null on unbalanced brackets or unterminated quotes. */
function splitTopLevel(text: string, sep: string): string[] | null {
  const parts: string[] = [];
  let depth = 0;
  let start = 0;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (c === '"' || c === "'" || c === '`') {
      let j = i + 1;
      while (j < text.length && text[j] !== c) j += text[j] === '\\' ? 2 : 1;
      if (j >= text.length) return null;
      i = j;
    } else if (c === '(' || c === '[' || c === '{') {
      depth++;
    } else if (c === ')' || c === ']' || c === '}') {
      depth--;
      if (depth < 0) return null;
    } else if (c === sep && depth === 0) {
      parts.push(text.slice(start, i));
      start = i + 1;
    }
  }
  if (depth !== 0) return null;
  parts.push(text.slice(start));
  return parts;
}

/** A whole JMESPath value literal (`` `json` `` or `'raw'`) → its value;
 * undefined when `expr` is anything else. */
function parseValueLiteral(expr: string): { value: any } | undefined {
  const quote = expr[0];
  if ((quote !== '`' && quote !== "'") || expr.length < 2 || expr[expr.length - 1] !== quote) return undefined;
  // The literal must span the whole value (`'a' || 'b'` is not one literal).
  let j = 1;
  while (j < expr.length && expr[j] !== quote) j += expr[j] === '\\' ? 2 : 1;
  if (j !== expr.length - 1) return undefined;
  const body = expr.slice(1, -1);
  if (quote === "'") return { value: body.replace(/\\'/g, "'") };
  try {
    return { value: JSON.parse(body.replace(/\\`/g, '`')) };
  } catch {
    return undefined;
  }
}

/** The fields input equivalent to a whole-input expression, or null when the
 * expression is not a top-level multiselect hash `{key: expr, ...}`. */
export function expressionToInput(expr: string): Record<string, any> | null {
  const text = expr.trim();
  if (text === '') return {};
  // A whole JSON-literal object (`{"a": 1}`) is a fields input of literals.
  const whole = parseValueLiteral(text);
  if (whole) return isPlainObject(whole.value) && !hasTemplateKeys(whole.value) ? whole.value : null;
  if (!text.startsWith('{') || !text.endsWith('}')) return null;
  if (!splitTopLevel(text, '\u0000')) return null; // unbalanced / unterminated
  // The opening brace must close at the very end: `{a: x}.a` is not a hash.
  let depth = 0;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (c === '"' || c === "'" || c === '`') {
      let j = i + 1;
      while (j < text.length && text[j] !== c) j += text[j] === '\\' ? 2 : 1;
      i = j;
    } else if (c === '(' || c === '[' || c === '{') depth++;
    else if (c === ')' || c === ']' || c === '}') {
      depth--;
      if (depth === 0 && i !== text.length - 1) return null;
    }
  }
  const entries = splitTopLevel(text.slice(1, -1), ',');
  if (!entries) return null;
  const input: Record<string, any> = {};
  for (const entry of entries) {
    const kv = splitTopLevel(entry, ':');
    if (!kv || kv.length < 2) return null;
    const rawKey = kv[0].trim();
    const value = kv.slice(1).join(':').trim();
    if (!value) return null;
    let key: string;
    if (IDENT_RE.test(rawKey)) key = rawKey;
    else if (rawKey.startsWith('"')) {
      try {
        key = JSON.parse(rawKey);
      } catch {
        return null;
      }
      if (typeof key !== 'string') return null;
    } else return null;
    // As a fields key, a trailing `.$` would turn it into an expression key.
    if (key.endsWith('.$')) return null;
    if (key in input || `${key}.$` in input) return null;
    const literal = parseValueLiteral(value);
    // A literal object holding `.$` keys would be *evaluated* as a template,
    // unlike the JMESPath literal — keep those as expressions.
    if (literal && !hasTemplateKeys(literal.value)) input[key] = literal.value;
    else input[`${key}.$`] = value;
  }
  return input;
}

// ---------------------------------------------------------------------------
// Expression context help
// ---------------------------------------------------------------------------

export interface InputField {
  name: string;
  type?: string;
  required: boolean;
}

/** The run-input fields a pipeline declares in its inputSchema. */
export function inputSchemaFields(schema: any): InputField[] {
  if (!isPlainObject(schema) || !isPlainObject(schema.properties)) return [];
  const required: string[] = Array.isArray(schema.required) ? schema.required : [];
  return Object.entries(schema.properties).map(([name, prop]: [string, any]) => ({
    name,
    type: Array.isArray(prop?.type) ? prop.type.join(' | ') : prop?.type,
    required: required.includes(name),
  }));
}

function fieldPath(root: string, name: string): string {
  return IDENT_RE.test(name) ? `${root}.${name}` : `${root}.${JSON.stringify(name)}`;
}

export interface SuggestionGroup {
  label: string;
  options: string[];
}

const OUTPUT_FIELDS: Partial<Record<StepType, string[]>> = {
  connector: ['body', 'statusCode'],
  query: ['rows', 'rowCount'],
  load: ['upserted'],
};

/** Insertable references for the expression picker at step `index`. */
export function expressionSuggestions(steps: Step[], index: number, inputSchema?: any): SuggestionGroup[] {
  const groups: SuggestionGroup[] = [
    { label: 'Run input', options: ['input', ...inputSchemaFields(inputSchema).map((f) => fieldPath('input', f.name))] },
  ];
  const prior = steps.slice(0, index).filter((s) => s.name);
  if (prior.length) {
    groups.push({
      label: 'Earlier steps',
      options: prior.flatMap((s) => {
        const out = `steps.${s.name}.output`;
        return [out, ...(OUTPUT_FIELDS[s.type] ?? []).map((f) => `${out}.${f}`)];
      }),
    });
  }
  groups.push({ label: 'Cursor & run', options: ['cursor', 'run.id', 'run.triggerType', 'run.firedAt', 'run.userId'] });
  return groups;
}

/** A starter run-input object from the declared schema (defaults, else empty
 * values of the declared type). */
export function exampleInput(schema: any): Record<string, any> {
  const example: Record<string, any> = {};
  if (!isPlainObject(schema) || !isPlainObject(schema.properties)) return example;
  for (const [name, prop] of Object.entries(schema.properties) as [string, any][]) {
    // Boolean subschemas (`true` = anything) are valid JSON Schema.
    if (isPlainObject(prop) && 'default' in prop) example[name] = prop.default;
    else if (Array.isArray(prop?.enum) && prop.enum.length) example[name] = prop.enum[0];
    else {
      const type = Array.isArray(prop?.type) ? prop.type[0] : prop?.type;
      example[name] = type === 'number' || type === 'integer' ? 0
        : type === 'boolean' ? false
        : type === 'array' ? []
        : type === 'object' ? {}
        : type === 'null' ? null
        : '';
    }
  }
  return example;
}

// ---------------------------------------------------------------------------
// Light client-side validation (server stays authoritative)
// ---------------------------------------------------------------------------

export interface StepIssue {
  index: number;
  message: string;
}

const REF_FIELD: Partial<Record<StepType, string>> = {
  connector: 'connector',
  function: 'function',
  agent: 'agent',
  query: 'query',
};

export function validateSteps(steps: Step[]): StepIssue[] {
  const issues: StepIssue[] = [];
  const names = steps.map((s) => s.name);
  steps.forEach((step, i) => {
    if (!STEP_NAME_RE.test(step.name || '')) {
      issues.push({ index: i, message: 'Step name must match [a-zA-Z_][a-zA-Z0-9_-]*' });
    } else if (names.indexOf(step.name) !== i) {
      issues.push({ index: i, message: `Duplicate step name '${step.name}'` });
    }
    const refField = REF_FIELD[step.type];
    if (refField && !REF_RE.test(step[refField] || '')) {
      issues.push({ index: i, message: `${refField} must be a namespace/name reference` });
    }
    if (step.type === 'connector' && !step.operation) {
      issues.push({ index: i, message: 'operation is required' });
    }
    if (step.type === 'load') {
      if (!step.connection) issues.push({ index: i, message: 'connection is required' });
      if (!step.table) issues.push({ index: i, message: 'table is required' });
      if (!step['items.$']) issues.push({ index: i, message: 'items expression is required' });
      if (!step['primaryKey.$']) issues.push({ index: i, message: 'primaryKey expression is required' });
    }
    const cursorSteps = steps.filter((s) => s.cursor);
    if (cursorSteps.length > 1 && step.cursor && cursorSteps[0] !== step) {
      issues.push({ index: i, message: 'Only one step may declare cursor config' });
    }
  });
  return issues;
}

/** One-line summary for a collapsed step card. */
export function stepSummary(step: Step): string {
  switch (step.type) {
    case 'connector':
      return [step.connector, step.operation].filter(Boolean).join(' · ') || 'not configured';
    case 'function':
      return step.function || 'not configured';
    case 'agent':
      return step.agent || 'not configured';
    case 'query':
      return step.query || 'not configured';
    case 'load':
      return step.connection && step.table ? `${step.connection} → ${step.table}` : 'not configured';
    default:
      return '';
  }
}
