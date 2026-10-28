import React from 'react';
import { createChat, getChat, streamFromChannel, streamMessage, useClient } from '@sinas/sdk';
import type {
  ApprovalRequest,
  ChatSessionMessage,
  MessageContent,
  SSEChunk,
  ToolStatus,
} from '@sinas/sdk';

import { apiClient, getApiErrorMessage } from '../../lib/api';
import {
  answerResumeChannel,
  approvalResumeChannel,
  inputRequiredFromChunk,
  mergePendingInputs,
  toPendingInputs,
  type PendingInput,
} from '../../lib/chatPending';
import {
  TRAILING_POLLS_MS,
  addApproval,
  applyTextDelta,
  applyToolEnd,
  applyToolStart,
  approvalFromEvent,
  createLatestGate,
  nextPendingPollMs,
  textFromMessageChunk,
} from '../../lib/chatStream';
import { streamErrorMessage } from '../../lib/llmErrors';

/**
 * The console's chat session: @sinas/sdk's useChat (0.7), plus what the
 * console needs that the published hook doesn't do: "always allow" on an
 * approval, a multi-call approval round that resumes only after its last
 * decision, and ask_user questions (live and on reload) with answers that
 * resume the conversation. Streaming uses the SDK's own stream functions;
 * the approval and pending-input calls go through the console's API client.
 * State transitions live in lib/chatStream.ts and lib/chatPending.ts.
 */
export interface ConsoleChatResult {
  chatId: string | null;
  messages: ChatSessionMessage[];
  loading: boolean;
  streaming: boolean;
  error: string | null;
  pendingApprovals: ApprovalRequest[];
  pendingInputs: PendingInput[];
  toolStatus: ToolStatus[];
  send: (content: MessageContent) => void;
  approve: (toolCallId: string, options?: { alwaysAllow?: boolean }) => void;
  reject: (toolCallId: string) => void;
  /** Resolves true once the answer is recorded (the card can then clear). */
  answer: (toolCallId: string, answer: string) => Promise<boolean>;
  stop: () => void;
  clearError: () => void;
}

export function useConsoleChat(
  agentRef: string,
  options?: { chatId?: string; input?: Record<string, unknown> },
): ConsoleChatResult {
  const client = useClient();

  const [chatId, setChatId] = React.useState<string | null>(options?.chatId ?? null);
  const [messages, setMessages] = React.useState<ChatSessionMessage[]>([]);
  const [loading, setLoading] = React.useState(false);
  const [streaming, setStreaming] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [pendingApprovals, setPendingApprovals] = React.useState<ApprovalRequest[]>([]);
  const [pendingInputs, setPendingInputs] = React.useState<PendingInput[]>([]);
  const [toolStatus, setToolStatus] = React.useState<ToolStatus[]>([]);
  // Index into TRAILING_POLLS_MS while re-reading a chat whose question
  // went away server-side; its length means "not trailing".
  const [trailingPoll, setTrailingPoll] = React.useState(TRAILING_POLLS_MS.length);
  // Bumped after each poll so the next one is scheduled even when the poll's
  // refresh was superseded (and so changed nothing).
  const [pollTick, setPollTick] = React.useState(0);

  const abortRef = React.useRef<AbortController | null>(null);
  const chatIdRef = React.useRef<string | null>(chatId);
  chatIdRef.current = chatId;
  // Only the newest refresh (or turn) may write the server's state.
  const [gate] = React.useState(createLatestGate);

  /**
   * Re-read the server's state: messages, approvals, questions. Resolves
   * to the questions shown, or null when a newer refresh or turn
   * superseded this one (its result is then dropped).
   */
  const refresh = React.useCallback(
    async (id: string): Promise<PendingInput[] | null> => {
      const ticket = gate.next();
      const [detail, inputs] = await Promise.allSettled([getChat(client, id), apiClient.listPendingInputs(id)]);
      if (!gate.isCurrent(ticket)) return null;
      // Best-effort: what is already shown stays when a read fails.
      if (detail.status === 'fulfilled') {
        setMessages(detail.value.messages);
        setPendingApprovals(detail.value.pendingApprovals ?? []);
      }
      if (inputs.status === 'fulfilled') {
        const next = toPendingInputs(inputs.value);
        setPendingInputs(next);
        return next;
      }
      return null;
    },
    [client, gate],
  );

  // Load an existing chat on mount
  React.useEffect(() => {
    if (!options?.chatId) return;
    let cancelled = false;
    const id = options.chatId;
    const ticket = gate.next();

    setLoading(true);
    getChat(client, id)
      .then((detail) => {
        if (cancelled || !gate.isCurrent(ticket)) return;
        setChatId(detail.id);
        setMessages(detail.messages);
        setPendingApprovals(detail.pendingApprovals ?? []);
      })
      .catch((e) => {
        if (!cancelled) setError((e as Error).message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    // The questions the chat is suspended on. Best-effort: the chat itself
    // works without them.
    apiClient
      .listPendingInputs(id)
      .then((items) => {
        if (!cancelled && gate.isCurrent(ticket)) setPendingInputs(toPendingInputs(items));
      })
      .catch(() => {});

    return () => {
      cancelled = true;
    };
  }, [client, gate, options?.chatId]);

  // Cleanup on unmount
  React.useEffect(() => () => abortRef.current?.abort(), []);

  // While questions wait with no stream open, nothing announces that one
  // expired (the round then resumes server-side) or was answered elsewhere:
  // re-read the chat periodically and just after the earliest deadline,
  // then a few more times once a question went away, to show the reply.
  React.useEffect(() => {
    if (streaming || !chatId) return;
    let delay: number | null = null;
    if (pendingInputs.length > 0) delay = nextPendingPollMs(pendingInputs, Date.now());
    else if (trailingPoll < TRAILING_POLLS_MS.length) delay = TRAILING_POLLS_MS[trailingPoll];
    if (delay === null) return;
    const waiting = pendingInputs.map((p) => p.toolCallId);
    const timer = setTimeout(async () => {
      const now = await refresh(chatId);
      if (waiting.length === 0) setTrailingPoll((i) => i + 1);
      else if (now && waiting.some((id) => !now.some((p) => p.toolCallId === id))) setTrailingPoll(0);
      setPollTick((t) => t + 1);
    }, delay);
    return () => clearTimeout(timer);
  }, [chatId, streaming, pendingInputs, trailingPoll, pollTick, refresh]);

  const handleStreamChunk = React.useCallback((chunk: SSEChunk) => {
    // A question: its own event, or (older backends, inline turn) a typed
    // message event.
    const asked = inputRequiredFromChunk(chunk);
    if (asked) {
      setPendingInputs((prev) => mergePendingInputs(prev, [asked]));
      return;
    }
    const chatIdNow = chatIdRef.current || '';
    if (chunk.event === 'message') {
      const text = textFromMessageChunk(chunk.data);
      if (text) setMessages((prev) => applyTextDelta(prev, text, chatIdNow));
    } else if (chunk.event === 'tool_start') {
      try {
        const parsed = JSON.parse(chunk.data);
        setToolStatus((prev) => [
          ...prev,
          { toolCallId: parsed.tool_call_id, name: parsed.name, description: parsed.description, status: 'running' },
        ]);
        setMessages((prev) => applyToolStart(prev, parsed, chatIdNow));
      } catch {
        // ignore malformed tool_start events
      }
    } else if (chunk.event === 'tool_end') {
      try {
        const parsed = JSON.parse(chunk.data);
        setToolStatus((prev) =>
          prev.map((t) => (t.toolCallId === parsed.tool_call_id ? { ...t, status: 'complete' } : t)),
        );
        setMessages((prev) => applyToolEnd(prev, parsed));
      } catch {
        // ignore malformed tool_end events
      }
    } else if (chunk.event === 'approval_required') {
      const approval = approvalFromEvent(chunk.data);
      if (approval) setPendingApprovals((prev) => addApproval(prev, approval));
    } else if (chunk.event === 'error') {
      // The turn failed: why (the error envelope), with a hint for codes
      // whose message doesn't already say what to do. The reason is in the
      // transcript too, once the stream ends and the chat is re-read.
      setError(streamErrorMessage(chunk.data));
    }
  }, []);

  const streamCallbacks = React.useCallback(
    (id: string) =>
      [
        () => {
          setStreaming(false);
          setToolStatus([]);
          refresh(id);
        },
        (err: Error) => {
          setError(err.message);
          setStreaming(false);
          setToolStatus([]);
        },
      ] as const,
    [refresh],
  );

  // Follow the channel a resumed round streams on (approval or answer).
  const follow = React.useCallback(
    (id: string, channelId: string) => {
      abortRef.current?.abort();
      gate.next(); // a refresh still in flight is older than this turn
      setStreaming(true);
      setError(null);
      const [onDone, onError] = streamCallbacks(id);
      abortRef.current = streamFromChannel(client, id, channelId, handleStreamChunk, onDone, onError);
    },
    [client, gate, handleStreamChunk, streamCallbacks],
  );

  const send = React.useCallback(
    (content: MessageContent) => {
      (async () => {
        let activeChatId = chatIdRef.current;

        // Create the chat on the first send
        if (!activeChatId) {
          setLoading(true);
          setError(null);
          try {
            const result = await createChat(client, agentRef, options?.input);
            activeChatId = result.id;
            setChatId(result.id);
            chatIdRef.current = result.id;
          } catch (e) {
            setError((e as Error).message);
            setLoading(false);
            return;
          }
          setLoading(false);
        }

        abortRef.current?.abort();
        gate.next(); // a refresh still in flight is older than this turn
        setStreaming(true);
        setError(null);
        setPendingApprovals([]);
        setToolStatus([]);

        // Show the user message optimistically
        const displayContent = typeof content === 'string' ? content : JSON.stringify(content);
        setMessages((prev) => [
          ...prev,
          {
            id: `tmp-user-${Date.now()}`,
            chatId: activeChatId!,
            role: 'user',
            content: displayContent,
            toolCalls: null,
            toolCallId: null,
            name: null,
            createdAt: new Date().toISOString(),
          },
        ]);

        const [onDone, onError] = streamCallbacks(activeChatId);
        abortRef.current = streamMessage(client, activeChatId, content, handleStreamChunk, onDone, onError);
      })();
    },
    [client, agentRef, options?.input, gate, handleStreamChunk, streamCallbacks],
  );

  const decide = React.useCallback(
    (toolCallId: string, approved: boolean, alwaysAllow: boolean) => {
      const id = chatIdRef.current;
      if (!id) return;
      gate.next(); // a refresh in flight must not bring the card back
      setPendingApprovals((prev) => prev.filter((a) => a.toolCallId !== toolCallId));
      setError(null);

      (async () => {
        try {
          const result = await apiClient.approveToolCall(id, toolCallId, {
            approved,
            ...(approved && alwaysAllow ? { always_allow: true } : {}),
          });
          // Until the round's last decision there is nothing to stream:
          // the remaining cards stay up.
          const channelId = approvalResumeChannel(result);
          if (channelId) follow(id, channelId);
        } catch (e) {
          setError(getApiErrorMessage(e, 'Could not record the decision'));
          // Show what is actually still pending (expired, already decided…)
          refresh(id);
        }
      })();
    },
    [follow, gate, refresh],
  );

  const approve = React.useCallback(
    (toolCallId: string, opts?: { alwaysAllow?: boolean }) => decide(toolCallId, true, !!opts?.alwaysAllow),
    [decide],
  );

  const reject = React.useCallback((toolCallId: string) => decide(toolCallId, false, false), [decide]);

  const answer = React.useCallback(
    async (toolCallId: string, text: string): Promise<boolean> => {
      const id = chatIdRef.current;
      if (!id) return false;
      setError(null);
      try {
        const result = await apiClient.answerPendingInput(id, toolCallId, text);
        gate.next(); // a refresh in flight must not bring the question back
        setPendingInputs((prev) => prev.filter((p) => p.toolCallId !== toolCallId));
        // The last outstanding completion resumes the round on a fresh
        // channel; otherwise the round keeps waiting on the others.
        const channelId = answerResumeChannel(result);
        if (channelId) follow(id, channelId);
        return true;
      } catch (e) {
        setError(getApiErrorMessage(e, 'Could not send the answer'));
        refresh(id);
        return false;
      }
    },
    [follow, gate, refresh],
  );

  const stop = React.useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setStreaming(false);
  }, []);

  const clearError = React.useCallback(() => setError(null), []);

  return {
    chatId,
    messages,
    loading,
    streaming,
    error,
    pendingApprovals,
    pendingInputs,
    toolStatus,
    send,
    approve,
    reject,
    answer,
    stop,
    clearError,
  };
}
