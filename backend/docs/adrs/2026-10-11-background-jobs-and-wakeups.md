# ADR: Background work and wakeups — start, go idle, be woken

- **Status:** Accepted
- **Date:** 2026-10-11
- **Authors:** Kjeld Oostra (with Claude)
- **Related code:**
  - `backend/app/services/chat_wakeups.py` — delivery primitive, timers, watches, background runs, the `background` tool surface
  - `backend/app/models/chat_wakeup.py`, migration `b1g2w3a4k5e6` — the `chat_wakeups` table (timers and watches)
  - `backend/app/api/runtime/endpoints/watch.py` — `POST /watch/{token}`
  - `backend/app/queue/worker.py` — `execute_background_code_job` (function worker), `deliver_due_wakeups_job` (agent worker cron)
  - `backend/app/services/message_service.py` `_handle_tool_calls` — `run_in_background(wait=true)` as a deferred call
  - `backend/app/services/deferred_completions.py` — the `background_job` completer
  - `packages/way-of-working.yaml` — the skill's "work in the background, be woken" section

## Context

The loop built by the workbench, loop-controls and deferred-round work
(ADRs 2026-09-01, 2026-09-02) lets an agent act like a careful engineer at
a terminal, but only *while its turn is running*. Everything it starts, it
waits for: `execute_code` blocks the tool round (and a worker slot) until
the sandbox returns; waiting on anything outside Sinas means polling inside
the turn or giving up; and nothing external can resume a chat except a
person typing into it.

Claude Code and Cowork sessions do three things this loop could not:

1. start a long-running job, carry on with other work (or stop), and be
   handed the result when it finishes;
2. schedule their own check-in ("look at this again in 30 minutes") and go
   idle in between;
3. hand an external system a callback and be woken when it fires.

The pieces were there. The deferred-round machinery already parks a round
on an external event and resumes it (`complete()`), with two completers
registered (sub-agents, human input). Schedules and webhooks already target
agents, but each firing starts a *fresh* chat. A message posted to a chat
mid-turn is already injected at the next round boundary (loop controls).
What was missing was one primitive — *deliver a message into an existing
chat, whatever state it is in* — and three thin tools on top of it.

## Decision

### One delivery primitive

`chat_wakeups.deliver_to_chat(chat_id, content, source, api_key_id)`:

- if the chat's turn is running (chat lock held) **or** the chat is parked
  on a pending completion (an `ask_user` question, a suspended delegation,
  a background run being waited on), the message is **injected**: a
  user-role row the running loop drains at its next round boundary. A new
  turn here would be wrong twice over: two turns on one chat race, and the
  history rebuild would "repair" the parked round's open tool calls with
  synthetic errors before the real results land;
- otherwise a **new turn** starts through the queue on a fresh stream
  channel recorded as the chat's `active_channel_id`, so a client can
  reconnect the way it does after an approval or an answer;
- archived chats and inactive owners drop the delivery;
- the turn acts through the API key the creating turn acted through
  (`api_key_id` stored with the wakeup, bound around the enqueue), as the
  deferred-round checkpoint already does.

Every delivery is a user-role message with a bracketed prefix —
`[background job <id> finished: …]`, `[wakeup <id>] …`,
`[watch <id>: …]` — so the agent (taught by the skill) treats it as an
event, not as the user speaking. User role, because it is the only role
that starts a turn semantically and that every provider accepts
mid-conversation; the prefix carries the provenance.

### Three tools, one `background` system tool

- **`run_in_background(code, description, wait?)`** queues a sandbox run
  on the function worker queue (`execute_background_code_job`, same
  `code_execution.execute` path as `execute_code`, so the workbench is
  materialized and written files persist back). Without `wait` it returns
  the job id at once and the result is delivered into the chat when done
  (clipped to `background_job_result_max_chars`; the full result is kept in
  Redis for 24 h behind `background_job_status`). With `wait: true` the
  tool is a **deferred call**: the round suspends on a `background_job`
  pending completion — the job is enqueued only after the checkpoint row
  exists, like delegation children — and the job's result completes it.
  The deadline is `code_execution_timeout + background_job_grace_seconds`;
  past it the expiry sweep resumes the round with a timeout error that
  names the job. A crash inside the run still reports back as an error
  result; a job must never leave a round parked.

  The deferral is conditional on an argument. `_handle_tool_calls` already
  splits calls by `deferred_completer`; a tool may now add
  `deferred_if_arg`, naming the argument that must be truthy for the call
  to defer. One tool, two shapes, no second name.
- **`schedule_wakeup(delay_seconds, note)`** inserts a `timer` row
  (`chat_wakeups`). The agent worker sweeps due timers once a minute
  (`deliver_due_wakeups_job`, `unique=True`, `FOR UPDATE SKIP LOCKED`,
  marked delivered before delivering so a crash cannot double-fire) and
  delivers `[wakeup <id>] <note>`. Minute granularity is the contract;
  delays are capped by `wakeup_max_delay_seconds` (7 days).
- **`create_watch(description, expires_in_seconds?)`** inserts a `watch`
  row with a 32-byte random token and returns
  `{public_base_url}/watch/{token}`. `POST /watch/{token}` is
  unauthenticated — the token is the secret — caps the body at
  `watch_max_payload_bytes`, delivers it (JSON when it parses, text
  otherwise) as `[watch <id>: <description>]\n<payload>`, and answers 202,
  404 (unknown), 410 (expired or deleted) or 413. Watches expire after
  `watch_default_ttl_seconds` (a day) unless asked otherwise, capped at
  `watch_max_ttl_seconds`.

`list_wakeups` shows the chat's pending timers, live watches and recent
background jobs; `cancel_wakeup` and `delete_watch` retire them.

### Why not the existing schedules and webhooks

Both are *resources*: config-applied, named, owned, templated, targeting an
agent and starting a fresh chat per firing. A wakeup is the opposite: an
ephemeral, chat-scoped artefact the agent mints for itself mid-turn and
that dies with the chat (`ON DELETE CASCADE`). Bolting a `chat` target
onto schedules would have meant one-shot cron expressions, APScheduler
state for rows that live minutes, and the resource lifecycle (config
export, appliers, console pages) for things nobody should manage by hand.
Webhooks would have brought template rendering, dedup config and the
resource lifecycle for a URL that exists to be thrown away. A small table
and a sweep are the honest size of the feature.

### Why a queue job and not a completer-only design

The "wait" shape is the deferred-round pattern exactly, so it uses it. The
"don't wait" shape is new: nothing is parked, the turn may well have ended,
and the result has to *arrive*. That is the delivery primitive, which
timers and watches need anyway. Making both shapes one job with an
optional `pending_completion_id` keeps one worker body and one status
record.

## Consequences

- A long sandbox run no longer holds an agent worker slot, in either
  shape. The run holds a function worker slot instead, as a function does.
- Agents can stop. The way-of-working skill now says so: schedule, end the
  turn, be woken. The exemplar agent enables `background`.
- Deliveries into a running turn are read at the next round boundary, so
  a turn with no tool calls (one long model response) sees a wakeup only
  when it ends — and then a new turn starts, because the chat is idle by
  the time the sweep runs. Same as user injection today.
- `POST /watch/{token}` is a public, write-only endpoint. The token is
  unguessable, the payload is capped, the watch expires, and the delivery
  is text the agent reads as an event. There is no rate limit beyond the
  deployment's; a watch URL that leaks lets an outsider talk to that one
  chat until it is deleted — the skill says to delete it when done.
- Redis holds background-job status for 24 h; a status lookup is scoped to
  the chat that started the job.
- The function worker must run for `run_in_background` to execute. The
  all-in-one profile runs both workers; split deployments already run it
  for functions.

## Alternatives considered

- **Deliver as a system-role message.** Provider adapters hoist or reject
  mid-conversation system messages inconsistently, and a system message
  does not start a turn. The bracketed user message is explicit and
  portable.
- **Poll from inside the turn** (`sleep` in `execute_code`). Holds a
  sandbox and a worker slot for the whole wait and dies with the job
  timeout. The reason this ADR exists.
- **Reuse `PendingToolApproval`'s resume for "wait".** It re-enters the
  round *before* execution; a background run needs the result *as* the
  tool result. The deferred-completion checkpoint is the right shape and
  already exists.
