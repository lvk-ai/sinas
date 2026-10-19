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

/**
 * The console's chat session: @sinas/sdk's useChat (0.7), plus what the
 * console needs that the published hook doesn't do: "always allow" on an
 * approval, a multi-call approval round that resumes only after its last
 * decision, and ask_user questions (live and on reload) with answers that
 * resume the conversation. Streaming uses the SDK's own stream functions;
 * the approval and pending-input calls go through the console's API client.
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

  const abortRef = React.useRef<AbortController | null>(null);
  const chatIdRef = React.useRef<string | null>(chatId);
  chatIdRef.current = chatId;

  // The questions the chat is suspended on. Best-effort: the chat itself
  // still works without them.
  const loadPendingInputs = React.useCallback(async (id: string) => {
    try {
      setPendingInputs(toPendingInputs(await apiClient.listPendingInputs(id)));
    } catch {
      // keep what is shown
    }
  }, []);

  // Load an existing chat on mount
  React.useEffect(() => {
    if (!options?.chatId) return;
    let cancelled = false;
    const id = options.chatId;

    setLoading(true);
    getChat(client, id)
      .then((detail) => {
        if (cancelled) return;
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
    apiClient
      .listPendingInputs(id)
      .then((items) => {
        if (!cancelled) setPendingInputs(toPendingInputs(items));
      })
      .catch(() => {});

    return () => {
      cancelled = true;
    };
  }, [client, options?.chatId]);

  // Cleanup on unmount
  React.useEffect(() => () => abortRef.current?.abort(), []);

  const handleStreamChunk = React.useCallback((chunk: SSEChunk) => {
    // A question: its own event (queued turn, reconnect) or a typed
    // message event (inline turn).
    const asked = inputRequiredFromChunk(chunk);
    if (asked) {
      setPendingInputs((prev) => mergePendingInputs(prev, [asked]));
      return;
    }
    if (chunk.event === 'message') {
      const newContent = (() => {
        try {
          const parsed = JSON.parse(chunk.data);
          return parsed.content || '';
        } catch {
          return chunk.data;
        }
      })();
      if (!newContent || typeof newContent !== 'string') return;
      setMessages((prev) => {
        const last = prev[prev.length - 1];
        if (last && last.role === 'assistant' && !last.toolCalls && last.streaming) {
          const next = [...prev];
          next[next.length - 1] = { ...last, content: (last.content || '') + newContent };
          return next;
        }
        return [
          ...prev,
          {
            id: `tmp-text-${Date.now()}`,
            chatId: chatIdRef.current || '',
            role: 'assistant' as const,
            content: newContent,
            toolCalls: null,
            toolCallId: null,
            name: null,
            createdAt: new Date().toISOString(),
            streaming: true,
          },
        ];
      });
    } else if (chunk.event === 'tool_start') {
      try {
        const parsed = JSON.parse(chunk.data);
        setToolStatus((prev) => [
          ...prev,
          { toolCallId: parsed.tool_call_id, name: parsed.name, description: parsed.description, status: 'running' },
        ]);
        setMessages((prev) => {
          if (prev.some((m) => m.toolCalls?.some((tc) => tc.id === parsed.tool_call_id))) return prev;
          // Freeze the text message before it in place
          const next = prev.map((m, i) =>
            i === prev.length - 1 && m.streaming && m.role === 'assistant' && !m.toolCalls
              ? { ...m, streaming: false }
              : m,
          );
          return [
            ...next,
            {
              id: `tmp-tc-${parsed.tool_call_id}`,
              chatId: chatIdRef.current || '',
              role: 'assistant' as const,
              content: null,
              toolCalls: [
                {
                  id: parsed.tool_call_id,
                  type: 'function' as const,
                  function: { name: parsed.name, arguments: parsed.arguments || '{}' },
                  description: parsed.description,
                },
              ],
              toolCallId: null,
              name: null,
              createdAt: new Date().toISOString(),
              streaming: true,
            },
          ];
        });
      } catch {
        // ignore malformed tool_start events
      }
    } else if (chunk.event === 'tool_end') {
      try {
        const parsed = JSON.parse(chunk.data);
        setToolStatus((prev) =>
          prev.map((t) => (t.toolCallId === parsed.tool_call_id ? { ...t, status: 'complete' } : t)),
        );
        if (parsed.result !== undefined) {
          const resultContent = typeof parsed.result === 'string' ? parsed.result : JSON.stringify(parsed.result);
          setMessages((prev) =>
            prev.map((m) =>
              m.toolCalls?.some((tc) => tc.id === parsed.tool_call_id)
                ? {
                    ...m,
                    toolResults: {
                      ...m.toolResults,
                      [parsed.tool_call_id]: { content: resultContent, name: parsed.name },
                    },
                  }
                : m,
            ),
          );
        }
      } catch {
        // ignore malformed tool_end events
      }
    } else if (chunk.event === 'approval_required') {
      try {
        const raw = JSON.parse(chunk.data);
        const parsed: ApprovalRequest = {
          type: raw.type,
          toolCallId: raw.tool_call_id,
          functionNamespace: raw.function_namespace,
          functionName: raw.function_name,
          arguments: raw.arguments || {},
        };
        setPendingApprovals((prev) =>
          prev.some((a) => a.toolCallId === parsed.toolCallId) ? prev : [...prev, parsed],
        );
      } catch {
        // ignore malformed approval events
      }
    } else if (chunk.event === 'error') {
      try {
        const parsed = JSON.parse(chunk.data);
        setError(parsed.error || 'Stream error');
      } catch {
        setError(chunk.data || 'Stream error');
      }
    }
  }, []);

  // The server's state after a stream ends: messages, approvals, questions.
  const refresh = React.useCallback(
    async (id: string) => {
      try {
        const detail = await getChat(client, id);
        setMessages(detail.messages);
        setPendingApprovals(detail.pendingApprovals ?? []);
      } catch {
        // Best-effort: streamed content is already visible
      }
      await loadPendingInputs(id);
    },
    [client, loadPendingInputs],
  );

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
      setStreaming(true);
      setError(null);
      const [onDone, onError] = streamCallbacks(id);
      abortRef.current = streamFromChannel(client, id, channelId, handleStreamChunk, onDone, onError);
    },
    [client, handleStreamChunk, streamCallbacks],
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
    [client, agentRef, options?.input, handleStreamChunk, streamCallbacks],
  );

  const decide = React.useCallback(
    (toolCallId: string, approved: boolean, alwaysAllow: boolean) => {
      const id = chatIdRef.current;
      if (!id) return;
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
    [follow, refresh],
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
        setPendingInputs((prev) => prev.filter((p) => p.toolCallId !== toolCallId));
        // The last outstanding completion resumes the round on a fresh
        // channel; otherwise the round keeps waiting on the others.
        const channelId = answerResumeChannel(result);
        if (channelId) follow(id, channelId);
        return true;
      } catch (e) {
        setError(getApiErrorMessage(e, 'Could not send the answer'));
        loadPendingInputs(id);
        return false;
      }
    },
    [follow, loadPendingInputs],
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
