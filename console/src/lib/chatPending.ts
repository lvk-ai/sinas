// What a chat is waiting on from the user: tool approvals and ask_user
// questions. Pure functions (tested in tests/chat-pending.test.mjs).

/** An ask_user question the conversation is suspended on. */
export interface PendingInput {
  toolCallId: string;
  question: string;
  options: string[] | null;
  expiresAt: string | null;
}

const optionsOf = (v: unknown): string[] | null =>
  Array.isArray(v) && v.length > 0 && v.every((o) => typeof o === 'string') ? (v as string[]) : null;

/** One question from the API (GET /chats/{id}/pending-input or an
 *  input_required stream event), or null when it isn't one. */
export function toPendingInput(raw: unknown): PendingInput | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  if (typeof r.tool_call_id !== 'string' || !r.tool_call_id) return null;
  return {
    toolCallId: r.tool_call_id,
    question: typeof r.question === 'string' ? r.question : '',
    options: optionsOf(r.options),
    expiresAt: typeof r.expires_at === 'string' ? r.expires_at : null,
  };
}

export function toPendingInputs(raw: unknown): PendingInput[] {
  return Array.isArray(raw) ? raw.map(toPendingInput).filter((p): p is PendingInput => p !== null) : [];
}

/**
 * The question an SSE chunk announces, or null. A queued (keep-alive) turn
 * and a reconnected stream send it as its own `input_required` event; an
 * inline turn sends it as a `message` event whose data has
 * type "input_required". Both are handled.
 */
export function inputRequiredFromChunk(chunk: { event: string; data: string }): PendingInput | null {
  if (chunk.event !== 'input_required' && chunk.event !== 'message') return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(chunk.data);
  } catch {
    return null;
  }
  if (chunk.event === 'message' && (parsed as { type?: unknown } | null)?.type !== 'input_required') return null;
  return toPendingInput(parsed);
}

/** Adds questions not already listed (by tool call id), keeping order. */
export function mergePendingInputs(current: PendingInput[], incoming: PendingInput[]): PendingInput[] {
  const seen = new Set(current.map((p) => p.toolCallId));
  const added = incoming.filter((p) => !seen.has(p.toolCallId) && seen.add(p.toolCallId));
  return added.length ? [...current, ...added] : current;
}

/**
 * The stream channel to follow after an approval decision, or null when
 * there is nothing to follow yet. A round with several asked calls resumes
 * once, after its last decision: until then the response says
 * resume: "waiting". The resuming decision (approve or reject) says
 * "queued"; rejected calls get a rejection result the model continues from.
 */
export function approvalResumeChannel(result: { resume?: string | null; channel_id?: string | null }): string | null {
  if (result.resume === 'waiting') return null;
  return result.channel_id || null;
}

/**
 * The stream channel to follow after answering a question, or null. The
 * answer resumes the conversation only when it was the round's last
 * outstanding completion (resumed) or when that resume is being retried
 * (resume_pending); either way on the channel returned.
 */
export function answerResumeChannel(result: {
  resumed?: boolean;
  resume_pending?: boolean;
  channel_id?: string | null;
}): string | null {
  if (!result.resumed && !result.resume_pending) return null;
  return result.channel_id || null;
}

/**
 * How an approval card names the call. Pending approvals carry a namespace
 * that says the kind: "sinas" for a platform (system) tool, "tool" for any
 * other non-function tool (workbench, connectors, MCP, collections…) that an
 * agent rule gated, and a real namespace for a function.
 */
export function approvalSubject(functionNamespace: string, functionName: string): { kind: string; name: string } {
  if (functionNamespace === 'sinas') return { kind: 'Platform tool', name: functionName };
  if (functionNamespace === 'tool' || !functionNamespace) return { kind: 'Tool', name: functionName };
  return { kind: 'Function', name: `${functionNamespace}/${functionName}` };
}
