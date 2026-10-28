// Config apply (POST /config/apply): the request body and what a result
// says was (or would be) removed by a prune. Pure functions (tested in
// tests/config-apply.test.mjs).

export interface ConfigApplySummary {
  created: { [kind: string]: number };
  updated: { [kind: string]: number };
  unchanged: { [kind: string]: number };
  skipped?: { [kind: string]: number };
  deleted: { [kind: string]: number };
}

export interface ResourceChange {
  resourceType: string;
  resourceName: string;
  action: string; // create, update, delete, unchanged, skipped
  details?: string | null;
  changes?: Record<string, unknown> | null;
  reason?: string | null;
}

export interface ConfigApplyResponse {
  success: boolean;
  summary: ConfigApplySummary;
  changes: ResourceChange[];
  errors?: string[];
  warnings?: string[];
}

export interface ConfigApplyOptions {
  dryRun?: boolean;
  force?: boolean;
  /** Also remove what this config file applied earlier but no longer declares. */
  prune?: boolean;
}

export interface ConfigApplyBody {
  config: string;
  dryRun: boolean;
  force: boolean;
  prune: boolean;
}

export function configApplyBody(config: string, options: ConfigApplyOptions = {}): ConfigApplyBody {
  return {
    config,
    dryRun: !!options.dryRun,
    force: !!options.force,
    prune: !!options.prune,
  };
}

/** What a prune removed (or, in a dry run, would remove), per kind, kinds and names sorted. */
export function removalsByKind(changes: ResourceChange[] | undefined | null): Array<{ kind: string; names: string[] }> {
  const byKind = new Map<string, string[]>();
  for (const change of changes || []) {
    if (change.action !== 'delete') continue;
    const names = byKind.get(change.resourceType) || [];
    names.push(change.resourceName);
    byKind.set(change.resourceType, names);
  }
  return [...byKind.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([kind, names]) => ({ kind, names: [...names].sort((a, b) => a.localeCompare(b)) }));
}

export function removalCount(changes: ResourceChange[] | undefined | null): number {
  return (changes || []).filter((c) => c.action === 'delete').length;
}

/**
 * The warnings a prune adds for resources it kept although no longer
 * declared (a collection with files, a store with saved state): shown with
 * the removals so it's clear why they stay.
 */
export function keptByPrune(warnings: string[] | undefined | null): string[] {
  return (warnings || []).filter((w) => w.startsWith('Kept ') && w.includes('although no longer declared'));
}

/** The errors a prune reports for a removal it refused (something still uses it) or that failed. */
export function blockedByPrune(errors: string[] | undefined | null): string[] {
  return (errors || []).filter((e) => e.startsWith("Can't remove ") || e.startsWith('Error removing '));
}

export function pluralResources(n: number): string {
  return `${n} resource${n === 1 ? '' : 's'}`;
}
