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
  not_eligible: 'active again or retention changed',
  too_large: 'too large to archive safely',
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

/**
 * The agent form's error for the retention field, or null. Only a value the
 * user changed is checked: a stored value is sent back as it is, so an
 * unrelated edit never fails on it.
 */
export function retentionInputError(mode: RetentionMode, daysText: string, touched: boolean): string | null {
  if (!touched || mode !== 'days' || parseRetentionDays(daysText) !== null) return null;
  return `Enter a whole number of days, 1 to ${MAX_RETENTION_DAYS.toLocaleString('en-US')}.`;
}

export interface RunLimits {
  max_chats_per_run: number | null;
  max_run_seconds: number | null;
}

/**
 * What one run handles at most, or null when it doesn't matter (everything
 * eligible fits and there is no time limit).
 */
export function runLimitNote(limits: RunLimits, eligible?: number): string | null {
  const capped = !!limits.max_chats_per_run && (eligible === undefined || eligible > limits.max_chats_per_run);
  if (!capped && !limits.max_run_seconds) return null;
  const parts: string[] = [];
  if (limits.max_chats_per_run) parts.push(`up to ${plural(limits.max_chats_per_run, 'chat')}`);
  if (limits.max_run_seconds) parts.push(`for at most ${plural(Math.max(1, Math.round(limits.max_run_seconds / 60)), 'minute')}`);
  return `One run handles ${parts.join(', ')}; whatever is left goes in later runs.`;
}

/**
 * The preview's verdict. The counts are chats that are eligible, not a
 * promise: a run can be refused, capped, or skip a chat that is in use when
 * it gets there.
 */
export function previewSummary(
  totals: { chats: number; messages: number },
  wouldArchive: boolean,
  refusal: string | null,
): string {
  const one = totals.chats === 1;
  const eligible = `${plural(totals.chats, 'expired chat')} (${plural(totals.messages, 'message')}) ${one ? 'is' : 'are'} past ${one ? 'its' : 'their'} retention period`;
  if (refusal) return `${eligible}, but a run now would be refused: ${refusal}`;
  return `${eligible}. A run archives ${wouldArchive ? 'each, then deletes it' : 'nothing (there is no archive destination) and deletes them'}.`;
}

/** The "Run now" confirmation. */
export function confirmRunMessage(
  eligible: { chats: number; messages: number } | null,
  archived: boolean,
  limits: RunLimits,
): string {
  const what = eligible
    ? `Eligible as of the last preview: ${plural(eligible.chats, 'chat')} (${plural(eligible.messages, 'message')}).`
    : 'Every chat past its retention period is eligible.';
  const how = archived
    ? 'Each chat the run reaches is archived and then permanently deleted from the database.'
    : 'Each chat the run reaches is permanently deleted; there is no archive destination.';
  const limit = runLimitNote(limits, eligible?.chats);
  return [
    'Run chat retention now?',
    '',
    what,
    how,
    ...(limit ? [limit] : []),
    "Chats in use when the run reaches them are skipped. This can't be undone from the console.",
  ].join('\n');
}
