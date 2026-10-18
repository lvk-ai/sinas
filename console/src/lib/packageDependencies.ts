// Package dependencies (package.requires) in the console: how a requirement
// reads, what a preview's dependency plan says, and the values an install
// sends for the packages it installs first. Pure, so npm test covers it.
// docs/design/package-dependencies.md

import type { DependencyPlanEntry, PackageRequirement } from '../types';

/** "gmail >=1.2,<2", "mail (as mail-support)", "slack". */
export function formatRequirement(req: PackageRequirement): string {
  const range = req.version ? ` ${req.version}` : '';
  const instance = req.instance && req.instance !== req.name ? ` (as ${req.instance})` : '';
  return `${req.name}${range}${instance}`;
}

export interface PlanSummary {
  /** Installed first, in this order. */
  toInstall: DependencyPlanEntry[];
  satisfied: DependencyPlanEntry[];
  /** Missing, incompatible, circular or invalid: the install is refused. */
  blocked: DependencyPlanEntry[];
}

export function planSummary(entries: DependencyPlanEntry[] | undefined): PlanSummary {
  const all = entries ?? [];
  return {
    toInstall: all.filter((e) => e.status === 'install'),
    satisfied: all.filter((e) => e.status === 'satisfied'),
    blocked: all.filter((e) => e.status !== 'install' && e.status !== 'satisfied'),
  };
}

/** Why a blocked entry blocks, for its badge. */
export const BLOCKED_LABEL: Record<string, string> = {
  missing: 'Not available',
  incompatible: 'Incompatible',
  cycle: 'Circular',
  invalid: "Can't install",
};

/** A blank secret is left out: the server then keeps an existing secret of
 * that name (or reports it missing), where an empty string would overwrite
 * it with nothing. */
export function sentValues(
  values: Record<string, any> | undefined,
  declarations: { name: string; type?: string }[] | undefined,
): Record<string, any> | undefined {
  const secrets = new Set((declarations ?? []).filter((v) => v.type === 'secret').map((v) => v.name));
  const sent = Object.fromEntries(
    Object.entries(values ?? {}).filter(([k, v]) => !(secrets.has(k) && (v === '' || v == null))),
  );
  return Object.keys(sent).length > 0 ? sent : undefined;
}

/** dependencyVariables for a preview or install: the values entered for each
 * package the plan installs, keyed by its install name. Values for packages
 * no longer in the plan are left out. */
export function dependencyPayload(
  values: Record<string, Record<string, any>>,
  entries: DependencyPlanEntry[] | undefined,
): Record<string, Record<string, any>> | undefined {
  const out: Record<string, Record<string, any>> = {};
  for (const entry of planSummary(entries).toInstall) {
    const sent = sentValues(values[entry.install_name], entry.variables);
    if (sent) out[entry.install_name] = sent;
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

/** The defaults a package to install declares, to pre-fill its form. */
export function variableDefaults(declarations: { name: string; default?: any }[] | undefined): Record<string, any> {
  const out: Record<string, any> = {};
  for (const v of declarations ?? []) {
    if (v.default !== undefined && v.default !== null) out[v.name] = v.default;
  }
  return out;
}
