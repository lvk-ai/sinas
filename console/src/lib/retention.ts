// Chat retention: the agent form's retention field and the System page's
// Retention panel. Pure functions (tested in tests/retention.test.mjs).
//
// An agent's retention_days: null = the deployment default, 0 = keep
// forever, N = N days after the chat's last activity.

export type RetentionMode = 'default' | 'never' | 'days';

export function retentionMode(days: number | null | undefined): RetentionMode {
  if (days === null || days === undefined) return 'default';
  return days === 0 ? 'never' : 'days';
}

/** Longest retention the field accepts (100 years): anything above is a typo. */
export const MAX_RETENTION_DAYS = 36500;

/** A whole number of days, 1..MAX_RETENTION_DAYS, or null when the text isn't one. */
export function parseRetentionDays(text: string): number | null {
  const trimmed = text.trim();
  if (!/^\d+$/.test(trimmed)) return null;
  const n = Number(trimmed);
  return n >= 1 && n <= MAX_RETENTION_DAYS ? n : null;
}

/**
 * The retention_days to send for a choice. `undefined` = the days text
 * isn't valid yet (don't send). null = back to the default, 0 = keep forever.
 */
export function retentionDaysFor(mode: RetentionMode, daysText: string): number | null | undefined {
  if (mode === 'default') return null;
  if (mode === 'never') return 0;
  const n = parseRetentionDays(daysText);
  return n === null ? undefined : n;
}

/** The "Use default" option, with the default when the caller may see it. */
export function defaultOptionLabel(defaultDays: number | null | undefined, known: boolean): string {
  if (!known) return 'Use default';
  return defaultDays ? `Use default (${plural(defaultDays, 'day')})` : 'Use default (keep forever)';
}

/** What an agent's setting means, in a few words. */
export function describeRetention(
  days: number | null | undefined,
  defaultDays: number | null | undefined,
  known = true,
): string {
  if (days === 0) return 'Keep forever';
  if (days) return plural(days, 'day');
  if (!known) return 'Deployment default';
  return defaultDays ? `Default (${plural(defaultDays, 'day')})` : 'Default (keep forever)';
}

export const SKIP_REASONS: Record<string, string> = {
  parked: 'waiting on a round (ask_user, sub-agent, background run)',
  pending_approval: 'tool approval pending',
  wakeup: 'wakeup timer or watch pending',
  mcp_task: 'MCP task unfinished',
  running: 'turn running',
  background_job: 'background job running',
  claimed: 'handled by another runner',
  locked: 'row locked at delete time',
  changed: 'written to while archiving',
  gone: 'deleted meanwhile',
};

export function skipLabel(reason: string): string {
  return SKIP_REASONS[reason] ?? reason.replace(/_/g, ' ');
}

export function plural(n: number, unit: string): string {
  return `${n.toLocaleString('en-US')} ${unit}${n === 1 ? '' : 's'}`;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i++;
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[i]}`;
}

interface RunCounts {
  archived: number;
  deleted: number;
  messages_deleted: number;
  skipped: Record<string, number>;
  failed: Record<string, number>;
  more_remaining?: boolean;
}

const sum = (m: Record<string, number> | undefined) => Object.values(m ?? {}).reduce((a, b) => a + b, 0);

/** One line for a run's counts, e.g. "12 archived, 12 deleted (340 messages); 3 skipped". */
export function runSummary(counts: RunCounts | null | undefined): string {
  if (!counts) return 'No chats handled';
  const parts = [`${counts.archived.toLocaleString('en-US')} archived`, `${counts.deleted.toLocaleString('en-US')} deleted (${plural(counts.messages_deleted, 'message')})`];
  const extras: string[] = [];
  const skipped = sum(counts.skipped);
  const failed = sum(counts.failed);
  if (skipped) extras.push(`${skipped.toLocaleString('en-US')} skipped`);
  if (failed) extras.push(`${failed.toLocaleString('en-US')} failed`);
  if (counts.more_remaining) extras.push('more left for the next run');
  return parts.join(', ') + (extras.length ? `; ${extras.join(', ')}` : '');
}
