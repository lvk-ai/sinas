// Studio's concept layer over the raw API shapes: naming conventions for
// auto-created resources, input-schema <-> friendly rows, and the
// plain-language capability summary. Every derived string traces back to a
// stored field (see studio/README.md §4).
import type { Agent, Manifest, Schedule, Webhook } from './types';

export function kebab(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 50);
}

export function splitRef(ref: string): { namespace: string; name: string } {
  const idx = ref.indexOf('/');
  return idx === -1
    ? { namespace: 'default', name: ref }
    : { namespace: ref.slice(0, idx), name: ref.slice(idx + 1) };
}

export function prettyName(name: string): string {
  const bare = name.includes('/') ? name.slice(name.indexOf('/') + 1) : name;
  return bare.replaceAll(/[-_]/g, ' ').replace(/^./, (c) => c.toUpperCase());
}

export function initials(name: string): string {
  const words = prettyName(name).split(' ').filter(Boolean);
  return ((words[0]?.[0] ?? '') + (words[1]?.[0] ?? '')).toUpperCase() || '?';
}

// ---- Auto-created resource conventions ----

/** The assistant's own read-write file space: collection `<agent>-files` in its namespace. */
export function ownFilesRef(agent: Agent): string {
  return `${agent.namespace}/${agent.name}-files`;
}

/** The assistant's conversation memory: store `<agent>-memory` in its namespace. */
export function memoryRef(agent: Agent): string {
  return `${agent.namespace}/${agent.name}-memory`;
}

// ---- Inputs: friendly rows over input_schema ----

export interface InputRow {
  name: string;
  description: string;
  kind: 'text' | 'choice';
  choices: string[];
  required: boolean;
}

export function schemaToInputRows(schema: Record<string, any> | null | undefined): InputRow[] {
  const props = schema?.properties ?? {};
  const required: string[] = schema?.required ?? [];
  return Object.entries(props).map(([name, def]: [string, any]) => ({
    name,
    description: def?.description ?? '',
    kind: Array.isArray(def?.enum) ? 'choice' : 'text',
    choices: Array.isArray(def?.enum) ? def.enum.map(String) : [],
    required: required.includes(name),
  }));
}

export function inputRowsToSchema(rows: InputRow[]): Record<string, any> {
  if (rows.length === 0) return {};
  const properties: Record<string, any> = {};
  for (const row of rows) {
    properties[row.name] = {
      type: 'string',
      ...(row.description ? { description: row.description } : {}),
      ...(row.kind === 'choice' && row.choices.length ? { enum: row.choices } : {}),
    };
  }
  const required = rows.filter((r) => r.required).map((r) => r.name);
  return { type: 'object', properties, ...(required.length ? { required } : {}) };
}

// ---- Plain-language derivations ----

const DAY_NAMES = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

export function cronToEnglish(cron: string): string {
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return cron;
  const [min, hour, dom, month, dow] = parts;
  const time = () =>
    `${Number(hour) % 12 === 0 ? 12 : Number(hour) % 12}:${min.padStart(2, '0')} ${Number(hour) < 12 ? 'AM' : 'PM'}`;
  if (/^\d+$/.test(min) && /^\d+$/.test(hour)) {
    if (dom === '*' && month === '*' && dow === '*') return `every day at ${time()}`;
    if (dow === '1-5' && dom === '*') return `every weekday at ${time()}`;
    if (/^\d+$/.test(dow) && dom === '*') return `every ${DAY_NAMES[Number(dow) % 7]} at ${time()}`;
    if (/^\d+$/.test(dom) && dow === '*') return `monthly on day ${dom} at ${time()}`;
  }
  if (min === '0' && hour === '*') return 'every hour';
  return cron;
}

/** Workflows that run a given assistant (source: schedule/webhook target fields). */
export function triggersForAgent(agent: Agent, schedules: Schedule[], webhooks: Webhook[]) {
  const scheduleHits = schedules.filter(
    (s) => s.schedule_type === 'agent' && s.target_namespace === agent.namespace && s.target_name === agent.name,
  );
  // Webhook -> adapter -> agent: the target assistant is a default_values
  // parameter on the webhook (see studio/README.md §5).
  const agentRef = `${agent.namespace}/${agent.name}`;
  const webhookHits = webhooks.filter((w) => w.default_values?.studio_agent === agentRef);
  return { scheduleHits, webhookHits };
}

/** Members of a project, grouped the way the project home displays them. */
export function projectMembers(manifest: Manifest) {
  const byType = (types: string[]) =>
    (manifest.required_resources ?? []).filter((r) => types.includes(r.type.replace(/s$/, '')));
  return {
    assistants: byType(['agent']),
    workflows: byType(['schedule', 'webhook']),
    other: (manifest.required_resources ?? []).filter(
      (r) => !['agent', 'schedule', 'webhook'].includes(r.type.replace(/s$/, '')),
    ),
  };
}
