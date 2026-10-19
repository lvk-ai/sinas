# ADR: Deferred tool rounds in inline turns

- **Status:** Accepted
- **Date:** 2026-10-20
- **Authors:** Kjeld Oostra (with Claude)
- **Extends:** [2026-09-02-deferred-tool-rounds-pending-completions.md](2026-09-02-deferred-tool-rounds-pending-completions.md)
- **Related code:**
  - `backend/app/services/message_service.py`: `_handle_tool_calls` (the deferral split and the round's channel), `run_turn` / `TurnResult`
  - `backend/app/services/sync_turns.py` (new): following a parked round for a synchronous caller, answering a question
  - `backend/app/api/runtime/endpoints/chats.py`: inline stream, `POST /chats/{id}/messages`, invoke, `POST /chats/{id}/pending-input/{tool_call_id}`
  - `backend/app/api/runtime/endpoints/webhooks.py` (sync agent webhooks), `backend/app/adapters/openai/endpoints.py`, `backend/app/services/pipeline_runner.py`
  - `backend/app/queue/agent_jobs.py`: `execute_agent_delegate_resume_job` reports its usage

## Context

The deferred-round ADR made a tool round suspend on calls whose result
arrives later (ask_user, a background run with `wait: true`, a sub-agent in
suspend mode) and resume when it lands. It only did this "when a job
channel exists", that is, in a turn run by a queue job (a keep-alive
chat). Every other way a turn starts runs it inside the HTTP request, with
no job and no channel:

- the default (non-keep-alive) streaming send,
- the non-streaming `POST /chats/{id}/messages`,
- `POST /agents/{ns}/{name}/invoke`,
- sync agent webhooks and the OpenAI adapter (both call the same service).

In those turns the deferred calls fell through to the executor, which
returned an error result ("ask_user is unavailable in this synchronous
context", "wait=true needs a queued turn"). Tool discovery still offered the
tools, so an agent with `askUser` was given a tool that always failed in
most chats. The docs even sent users to the invoke endpoint as a way to
get a queued turn, which it isn't.

There were two smaller gaps next to it. Invoking a session whose chat is
busy or parked returned the caller's own message as `reply`. And a pipeline
agent step that asked a question waited on a channel the answer would
never resume on, then failed with "did not respond".

## Decision

**A deferred call defers in every turn, inline or queued.** The split in
`_handle_tool_calls` no longer depends on a job channel for ask_user and
waited-on background runs. When the round suspends without one, it gets a
channel of its own: a fresh id stored on the checkpoint and recorded as the
chat's `active_channel_id`. That is the channel the round's continuation
streams on when it resumes by itself (a background run landing, a question
expiring); an answer still resumes on the fresh channel the answer endpoint
returns. From the checkpoint on, an inline-started round is the same as a
queued one: the same row, the same `complete()`, the same resume job, the
same expiry sweep. The resume job sets the job channel, so later rounds of
that conversation run on the queued path.

**Suspend-mode delegation stays blocking in an inline turn.** Suspend mode
exists to free the worker slot a waiting parent holds (issue #90). An
inline turn holds no worker slot. It holds a request, which the child's
reply finishes, so a synchronous caller still gets that reply in the same
response. The split takes delegations only when a job channel exists. The
mechanism would handle them (a mixed round already does in queued turns);
this is a choice, not a gap.

**Each entry point reports what the turn ended in**, using one vocabulary:

| status | meaning | the caller then |
|---|---|---|
| `completed` | final reply in the response | — |
| `input_required` | parked on ask_user question(s), listed in `pending_inputs` | answers with `POST /chats/{id}/pending-input/{tool_call_id}` |
| `in_progress` | goes on without this request (background work outlasted the request's time budget) | follows `channel_id` with `GET /chats/{id}/stream/{channel_id}`, or reads the chat later |
| `injected` | the chat was busy (a turn running, or a round parked), so the message was delivered into that turn | waits for that turn (`pending_inputs` / `channel_id` say what it waits on) |

- **Inline stream:** `input_required` and `round_suspended` (now carrying
  `channel_id`) arrive as their own SSE events, as on a keep-alive stream.
  A round parked on the user alone ends the stream with `done`
  `{"status": "suspended"}`. A round that waits on anything else keeps the
  stream open and relays the continuation from the round's channel until
  `done`, which is what a keep-alive stream does. When such a round also
  asked a question and the answer resumes it on a fresh channel,
  `complete()` publishes `round_moved` `{"channel_id": <fresh>}` on the
  round's own channel. Relays (keep-alive, reconnect, inline) and
  synchronous followers move to the new channel instead of idling on the
  old one. Clients never see `round_moved`.
- **Non-streaming send, invoke, sync webhook:** a round that waits on work
  that lands by itself is **followed** within the request's time budget,
  so a synchronous caller gets the final reply. The budget is the chat's
  job timeout for send, and the agent's for invoke and webhooks. Only a
  human question, or running out of budget, ends the request early.
  `MessageSendResponse` and `InvokeResponse` gain `status`,
  `pending_inputs` and `channel_id`. An injected invoke returns an empty
  `reply` instead of echoing the caller's message.
- **Answering:** the answer endpoint keeps its 202 contract
  (`channel_id` to stream the resumed turn from). It also takes
  `wait: true`, which returns the resumed turn's result in the same shape
  (200): the reply, the next question, or `in_progress`. This works the same
  for a round started inline or queued.
- **OpenAI adapter:** the wire format can only carry a message, so the
  question is rendered into the assistant text, and the next message in
  that chat (`X-Chat-ID`) answers the oldest open question.
  `X-Sinas-Turn-Status` carries the status on non-streaming responses.
- **Pipelines:** an agent step that sees `input_required` fails at once
  with the question in the error. A pipeline runs unattended, and the
  answer would resume on a channel the step does not follow. A step whose
  round waits on a background run keeps waiting, because that resumes on
  the step's channel.

**Usage stays per request.** `usage` covers the LLM calls made while the
request ran. For a followed continuation, which runs in the resume job (a
worker), the job collects its own usage and puts it on the event that ends
it on the channel (`done`, or the held-back `round_suspended`). The
follower adds those parts to the request's own. If a continuation reports
nothing (for example a worker from before this change), the total is
`null`, not an under-count. The same holds when the request's budget runs
out while a continuation is mid-run. The calls made after an answer belong to the
answer's request when it waits, and to no request when it doesn't. That is
the same as a keep-alive stream, which never reported usage.

**Locks and identity are unchanged.** The inline turn holds the chat lock
until it suspends and then releases it. A parked round makes later messages
inject (#282). The resume job takes the lock as before. The checkpoint
records the request's API key like a queued round does, so the
continuation acts through it whoever answers (#223).

### Why follow rather than resume in-process

A waited answer, or an invoke waiting on a background run, could run the
continuation in the API process to get usage by context for free. We
rejected that:

- There would be two continuation implementations. The resume job also
  closes batch `Execution` rows, reports to a waiting parent, and requeues
  itself when the chat is busy.
- The checkpoint invariant is "deleted only once its continuation is
  queued". An in-process claim races the expiry sweep's retry of
  completed-but-unqueued checkpoints.
- A delegated child in block mode must resume on the channel its parent
  waits on, which only the job publishes to.

Following the channel reuses the one path. The only extra piece is that the
job reports its usage on its terminal event, which the SSE relays never
forward (they rebuild `done`).

### Rejected alternatives

- **Hide the tools in inline turns** (strip `ask_user` from discovery
  unless keep-alive). It removes the error but leaves the feature unusable
  on most entry points, and a chat's tool list would depend on how a
  message was sent.
- **Make every turn queued** (inline sends enqueue and subscribe). That is
  a much larger change to latency, failure modes and the inline-disconnect
  semantics, made to fix one tool round.
- **Answer a parked question with the next plain message, everywhere.**
  It is natural for chat integrations, but it changes the console's
  keep-alive contract, where a typed message steers the turn rather than
  answering the open card. It is kept to the OpenAI adapter, which has no
  other way to answer. (The 09-02 ADR's next step 3 is still open for the
  other entry points.)
- **A separate "answer" field on invoke.** The answer endpoint with
  `wait: true` already gives sync callers a one-request answer with the
  same response shape, for every entry point.

## Impact

| Component | Change |
|---|---|
| `_handle_tool_calls` | ask_user and waited-on runs always defer; inline rounds get their own channel (stored on the checkpoint, set as `active_channel_id`); `round_suspended` carries `channel_id` |
| `MessageService.run_turn` | New: `send_message` with a `TurnResult` (status, message, channel). `send_message` returns `.message` as before |
| `sync_turns` | New: `RoundFollower`, `settle_turn`, `answer_question`, `settle_answer`, `combine_usage` |
| Chat API | Inline stream events/ending; `status`/`pending_inputs`/`channel_id` on send and invoke; `wait` on the answer endpoint |
| Sync webhooks | `status` on the response, plus `pending_inputs`/`channel_id` when not completed |
| OpenAI adapter | Questions rendered as text; next message answers; `X-Sinas-Turn-Status` |
| Pipeline agent step | Fails clearly on `input_required` |
| Round-resume job | Collects its usage; `done` and `round_suspended` carry it |
| Executor fallbacks | Unreachable from the chat loop; reworded as guards for callers outside a tool round |

## Open questions

- **Approvals in non-streaming turns** still refuse ("use streaming
  mode"). The same vocabulary would carry an `approval_required` status
  with `pending_approvals`, and the approval resume job would need to report
  usage the same way. Left as a follow-up.
- **Usage after a 202 answer** is not reported anywhere but `llm_usage`. A
  stream-level usage event for keep-alive and resumed streams would close
  that.

## What we'd NOT do

- Change the keep-alive path, its events, or the console's contract.
- Defer suspend-mode delegations in inline turns (see above).
