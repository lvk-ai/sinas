// An agent's permission mode (tool_approvals): which tool calls stop for the
// user's approval. Pure functions (tested in tests/tool-approvals.test.mjs).
//
// Shape (backend app/services/approval_rules.validate_tool_approvals):
//   {"default": "auto" | "ask", "rules": [{"match": "<glob>", "action": "auto" | "ask"}]}
// Resolution for one call: a per-chat "always allow" grant, then the first
// matching rule, then the tool's own requiresApproval, then the default.

export type ToolApprovalAction = 'auto' | 'ask';

export interface ToolApprovalRule {
  match: string;
  action: ToolApprovalAction;
}

export interface ToolApprovals {
  default: ToolApprovalAction;
  rules: ToolApprovalRule[];
}

const isAction = (v: unknown): v is ToolApprovalAction => v === 'auto' || v === 'ask';

/** The editor's state for a stored value. Unset (or anything malformed) is
 *  the backend's own default: run everything, no rules. */
export function toolApprovalsDraft(value: unknown): ToolApprovals {
  const obj = value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
  const rules = Array.isArray(obj.rules) ? obj.rules : [];
  return {
    default: isAction(obj.default) ? obj.default : 'auto',
    rules: rules
      .filter((r): r is Record<string, unknown> => !!r && typeof r === 'object')
      .map((r) => ({
        match: typeof r.match === 'string' ? r.match : '',
        action: isAction(r.action) ? r.action : 'ask',
      })),
  };
}

/** Whether a draft means the same as no setting at all. */
export function isUnsetEquivalent(draft: ToolApprovals): boolean {
  return draft.default === 'auto' && draft.rules.length === 0;
}

/** Why the draft can't be saved, or null. The backend rejects a rule
 *  without a pattern, so it is caught here, with the rule it is. */
export function toolApprovalsError(draft: ToolApprovals): string | null {
  const blank = draft.rules.findIndex((r) => !r.match.trim());
  return blank >= 0 ? `Rule ${blank + 1} needs a tool name pattern.` : null;
}

/**
 * The tool_approvals to send on save, or undefined to leave it out.
 *
 * Left out when the agent has no setting and the draft still means "no
 * setting": an untouched agent saves without one. The update API ignores an
 * explicit null, so clearing a stored setting sends the empty form
 * ({"default": "auto", "rules": []}), which behaves exactly as unset.
 */
export function toolApprovalsPayload(draft: ToolApprovals, stored: unknown): ToolApprovals | undefined {
  const normalized: ToolApprovals = {
    default: draft.default,
    rules: draft.rules.map((r) => ({ match: r.match.trim(), action: r.action })),
  };
  const hadSetting = stored !== null && stored !== undefined;
  if (!hadSetting && isUnsetEquivalent(normalized)) return undefined;
  return normalized;
}

/** The rules with the one at `index` moved one place up (-1) or down (+1). */
export function moveRule(rules: ToolApprovalRule[], index: number, delta: -1 | 1): ToolApprovalRule[] {
  const target = index + delta;
  if (index < 0 || index >= rules.length || target < 0 || target >= rules.length) return rules;
  const next = [...rules];
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}
