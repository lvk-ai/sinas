// The console chat session's state transitions, as pure functions so they
// can be tested without React (tests/chat-stream.test.mjs). The hook
// (components/chat/useConsoleChat.ts) only wires them to state.
import type { ApprovalRequest, ChatSessionMessage } from '@sinas/sdk';

import type { PendingInput } from './chatPending';

/** The text a `message` stream event carries, or '' when it carries none
 *  (a typed event such as input_required or tool_rejected). */
export function textFromMessageChunk(data: string): string {
  try {
    const parsed = JSON.parse(data);
    return parsed && typeof parsed.content === 'string' ? parsed.content : '';
  } catch {
    return data;
  }
}

/** Appends streamed text to the assistant message being streamed, or starts one. */
export function applyTextDelta(
  messages: ChatSessionMessage[],
  text: string,
  chatId: string,
  now: Date = new Date(),
): ChatSessionMessage[] {
  if (!text) return messages;
  const last = messages[messages.length - 1];
  if (last && last.role === 'assistant' && !last.toolCalls && last.streaming) {
    const next = [...messages];
    next[next.length - 1] = { ...last, content: (last.content || '') + text };
    return next;
  }
  return [
    ...messages,
    {
      id: `tmp-text-${now.getTime()}`,
      chatId,
      role: 'assistant',
      content: text,
      toolCalls: null,
      toolCallId: null,
      name: null,
      createdAt: now.toISOString(),
      streaming: true,
    },
  ];
}

export interface ToolStartEvent {
  tool_call_id: string;
  name: string;
  arguments?: string;
  description?: string;
}

/** Shows a started tool call as its own bubble (once), freezing the text before it. */
export function applyToolStart(
  messages: ChatSessionMessage[],
  event: ToolStartEvent,
  chatId: string,
  now: Date = new Date(),
): ChatSessionMessage[] {
  if (messages.some((m) => m.toolCalls?.some((tc) => tc.id === event.tool_call_id))) return messages;
  const frozen = messages.map((m, i) =>
    i === messages.length - 1 && m.streaming && m.role === 'assistant' && !m.toolCalls ? { ...m, streaming: false } : m,
  );
  return [
    ...frozen,
    {
      id: `tmp-tc-${event.tool_call_id}`,
      chatId,
      role: 'assistant',
      content: null,
      toolCalls: [
        {
          id: event.tool_call_id,
          type: 'function',
          function: { name: event.name, arguments: event.arguments || '{}' },
          description: event.description,
        },
      ],
      toolCallId: null,
      name: null,
      createdAt: now.toISOString(),
      streaming: true,
    },
  ];
}

/** Merges a finished tool call's result into its bubble. */
export function applyToolEnd(
  messages: ChatSessionMessage[],
  event: { tool_call_id: string; name: string; result?: unknown },
): ChatSessionMessage[] {
  if (event.result === undefined) return messages;
  const content = typeof event.result === 'string' ? event.result : JSON.stringify(event.result);
  return messages.map((m) =>
    m.toolCalls?.some((tc) => tc.id === event.tool_call_id)
      ? { ...m, toolResults: { ...m.toolResults, [event.tool_call_id]: { content, name: event.name } } }
      : m,
  );
}

/** An approval_required event as an approval card, or null when malformed. */
export function approvalFromEvent(data: string): ApprovalRequest | null {
  try {
    const raw = JSON.parse(data);
    if (!raw || typeof raw.tool_call_id !== 'string') return null;
    return {
      type: raw.type,
      toolCallId: raw.tool_call_id,
      functionNamespace: raw.function_namespace,
      functionName: raw.function_name,
      arguments: raw.arguments || {},
    };
  } catch {
    return null;
  }
}

/** Adds an approval card unless that call already has one. */
export function addApproval(current: ApprovalRequest[], approval: ApprovalRequest): ApprovalRequest[] {
  return current.some((a) => a.toolCallId === approval.toolCallId) ? current : [...current, approval];
}

/**
 * Orders asynchronous refreshes: each one takes a ticket, and only the
 * newest ticket may write its result. A slow refresh from an earlier turn
 * then can't overwrite what a later turn (or a later refresh) showed.
 * Starting a turn also takes a ticket, voiding refreshes still in flight.
 */
export function createLatestGate() {
  let latest = 0;
  return {
    next: (): number => ++latest,
    isCurrent: (ticket: number): boolean => ticket === latest,
  };
}

/** Never wait longer than this between checks while a question is open. */
export const PENDING_POLL_MAX_MS = 30_000;
const PENDING_POLL_MIN_MS = 1_000;
/** Checked this long after a deadline (the expiry sweep runs about once a minute). */
const EXPIRY_GRACE_MS = 5_000;
/** Once a deadline has passed, how often to check until the sweep has run. */
const OVERDUE_POLL_MS = 10_000;

/**
 * How long to wait before re-reading a chat that waits on ask_user
 * questions with no stream open. An unanswered question expires and the
 * round resumes server-side without the console being told; re-reading
 * every PENDING_POLL_MAX_MS, and just after the earliest deadline, shows
 * that (and an answer given elsewhere) without a reload.
 */
export function nextPendingPollMs(inputs: PendingInput[], nowMs: number): number {
  let wait = PENDING_POLL_MAX_MS;
  for (const input of inputs) {
    const deadline = input.expiresAt ? Date.parse(input.expiresAt) : NaN;
    if (Number.isNaN(deadline)) continue;
    const untilDue = deadline + EXPIRY_GRACE_MS - nowMs;
    wait = Math.min(wait, untilDue > 0 ? untilDue : OVERDUE_POLL_MS);
  }
  return Math.max(PENDING_POLL_MIN_MS, wait);
}

/** After a question disappears without an answer from here (it expired, or
 *  was answered elsewhere), the round resumes server-side: re-read the chat
 *  at these delays to show the reply. */
export const TRAILING_POLLS_MS = [5_000, 15_000, 30_000];
