// Identity attribution in the console: an LLM provider's header templates
// (config.headers) and an identity provider's claim mappings. Pure functions
// (tested in tests/attribution.test.mjs). The backend validates the same
// rules (app/providers/attribution.py, app/schemas/spec/identity_provider.py);
// these only point out problems before saving.

/** The variables a header template may use, with what they hold. */
export const HEADER_TEMPLATE_VARIABLES: Array<{ name: string; help: string }> = [
  { name: '{{attribution.<key>}}', help: "an attribution field from the chat's snapshot, e.g. an organization id" },
  { name: '{{user.id}}', help: "the id of the user the call runs for (the chat's owner)" },
  { name: '{{user.email}}', help: 'their email address (personal data: leaves your infrastructure)' },
  { name: '{{user.custom_fields.<key>}}', help: 'one of their custom fields, as it is now' },
  { name: '{{agent.namespace}}', help: "the agent's namespace" },
  { name: '{{agent.name}}', help: "the agent's name" },
  { name: '{{chat.id}}', help: 'the chat (left out of provider batches)' },
  { name: '{{api_key.name}}', help: 'the API key the turn acts through, if any' },
];

const FIXED_VARIABLES = new Set([
  'user.id', 'user.email', 'agent.namespace', 'agent.name', 'chat.id', 'api_key.name',
]);
const FIELD_KEY = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;
const HEADER_NAME = /^[A-Za-z0-9][A-Za-z0-9-]{0,99}$/;
const PLACEHOLDER = /\{\{\s*([^{}]*?)\s*\}\}/g;
const PRINTABLE = /^[\x20-\x7e]*$/;

const RESERVED = new Set([
  'authorization', 'proxy-authorization', 'x-api-key', 'api-key', 'cookie', 'set-cookie', 'host',
  'content-length', 'content-type', 'content-encoding', 'transfer-encoding', 'connection', 'keep-alive',
  'te', 'trailer', 'upgrade', 'expect', 'accept', 'accept-encoding', 'user-agent', 'forwarded',
  'x-forwarded-for', 'x-forwarded-host', 'x-forwarded-proto', 'x-real-ip', 'idempotency-key',
  'x-should-retry', 'ocp-apim-subscription-key', 'x-portkey-api-key', 'cf-aig-authorization',
  'traceparent', 'tracestate', 'baggage', 'b3', 'uber-trace-id', 'sentry-trace',
  'x-cloud-trace-context', 'grpc-trace-bin', 'x-amzn-trace-id',
]);
const RESERVED_PREFIXES = [
  'x-sinas-', 'anthropic-', 'x-stainless-', 'x-goog-', 'x-amz-', 'proxy-', 'sec-', 'x-b3-',
  'uberctx-', 'ot-tracer-', 'x-datadog-',
];

export interface HeaderRow {
  name: string;
  template: string;
}

/** The rows the editor shows for a provider's config. */
export function headerRowsFromConfig(config: Record<string, unknown> | null | undefined): HeaderRow[] {
  const headers = config?.headers;
  if (!headers || typeof headers !== 'object' || Array.isArray(headers)) return [];
  return Object.entries(headers as Record<string, unknown>).map(([name, template]) => ({
    name,
    template: typeof template === 'string' ? template : String(template ?? ''),
  }));
}

/** `config` with its headers replaced by the rows (none: no `headers` key). */
export function configWithHeaders(
  config: Record<string, unknown> | null | undefined,
  rows: HeaderRow[],
): Record<string, unknown> {
  const { headers: _previous, ...rest } = config ?? {};
  void _previous;
  const kept = rows.filter((r) => r.name.trim() !== '' || r.template.trim() !== '');
  if (kept.length === 0) return rest;
  return { ...rest, headers: Object.fromEntries(kept.map((r) => [r.name.trim(), r.template])) };
}

function variableProblem(name: string): string | null {
  if (FIXED_VARIABLES.has(name)) return null;
  for (const prefix of ['user.custom_fields.', 'attribution.']) {
    if (name.startsWith(prefix)) {
      const key = name.slice(prefix.length);
      return FIELD_KEY.test(key) ? null : `'${key}' is not a field key`;
    }
  }
  return `Unknown variable {{${name}}}`;
}

/** One problem per row (or null), as the backend would refuse it. */
export function headerRowProblems(rows: HeaderRow[]): Array<string | null> {
  const seen = new Map<string, number>();
  rows.forEach((r, i) => {
    const key = r.name.trim().toLowerCase();
    if (key && !seen.has(key)) seen.set(key, i);
  });
  return rows.map((row, i) => {
    const name = row.name.trim();
    if (!name && !row.template.trim()) return null; // an empty row is dropped on save
    if (!HEADER_NAME.test(name)) return 'Header names are letters, digits and dashes.';
    const lowered = name.toLowerCase();
    if (seen.get(lowered) !== i) return 'This header is set twice.';
    if (RESERVED.has(lowered) || RESERVED_PREFIXES.some((p) => lowered.startsWith(p))) {
      return 'Reserved: credentials, framing, trace context and SDK headers can’t be set here.';
    }
    if (!row.template.trim()) return 'Enter a template.';
    if (row.template.length > 1024) return 'At most 1024 characters.';
    if (!PRINTABLE.test(row.template)) return 'Printable ASCII only (no line breaks).';
    const statics = row.template.replace(PLACEHOLDER, '');
    if (statics.includes('{{') || statics.includes('}}')) return "Unbalanced '{{' or '}}'.";
    for (const match of row.template.matchAll(PLACEHOLDER)) {
      const problem = variableProblem(match[1]);
      if (problem) return problem;
    }
    return null;
  });
}

/** The first problem with a config's header templates, or null. */
export function headerProblem(config: Record<string, unknown> | null | undefined): string | null {
  return headerRowProblems(headerRowsFromConfig(config)).find((p) => p !== null) ?? null;
}

export interface ClaimMappingRow {
  claim: string;
  field: string;
}

/** One problem per claim mapping (or null), as the backend would refuse it. */
export function claimMappingProblems(rows: ClaimMappingRow[]): Array<string | null> {
  const seen = new Map<string, number>();
  rows.forEach((r, i) => {
    const key = r.field.trim();
    if (key && !seen.has(key)) seen.set(key, i);
  });
  return rows.map((row, i) => {
    const claim = row.claim.trim();
    const field = row.field.trim();
    if (!claim) return 'Enter the claim.';
    if (claim.startsWith('.') || claim.endsWith('.') || /\s/.test(claim)) return 'Not a claim path.';
    if (!field) return 'Enter the custom field.';
    if (!FIELD_KEY.test(field)) return 'Custom fields are letters, digits and _, not starting with a digit.';
    if (seen.get(field) !== i) return 'This field is mapped twice.';
    return null;
  });
}
