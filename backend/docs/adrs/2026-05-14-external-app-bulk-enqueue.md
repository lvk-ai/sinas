# ADR: Bulk function/agent enqueue for external apps

- **Status:** Proposed
- **Date:** 2026-05-14
- **Authors:** Kjeld + Claude
- **Related code:**
  - `backend/app/services/queue_service.py` (`enqueue_function`)
  - `backend/app/services/execution_engine.py` (`execute_function`, token minting)
  - `backend/app/core/auth.py` (`create_access_token`, `access_token_expire_minutes`)
  - `backend/app/api/v1/endpoints/functions.py` (target for the new endpoint)
  - **Concrete consumer:** `grove/backend/app/services/sinas.py`
    (`get_admin_client()` — the pattern being replaced)

## 1. Context

External apps — Grove most concretely — want to run **functions and agents
in bulk on behalf of users**: a user kicks off an ingestion run, Grove fans
that out into N function executions on Sinas, each potentially invoking
agents that themselves invoke sub-agents.

Today the only way for Grove to start this work is:

```python
client = get_admin_client()   # SINAS_API_KEY, broad permissions
client._request("POST", "/queries/.../execute", ...)
```

Three problems:

1. **No public bulk-enqueue surface.** `queue_service.enqueue_function` is
   internal; apps fall back to synchronous calls or have to build their own
   queue.
2. **Wrong attribution.** Every call shows up as the admin service-key, not
   the user who triggered it. Audit trails dead-end at "Grove."
3. **Token TTL is too short for long pipelines.** `create_access_token` uses
   the default `access_token_expire_minutes` (15). A function that runs 14
   minutes, then calls Grove, then has Grove make a follow-up Sinas call,
   can blow past the ceiling.

What **doesn't** turn out to be a problem on closer inspection:

- **Cross-app token forwarding** already works. Sinas's per-execution access
  token is signed with the instance's `SECRET_KEY`. Grove's `SinasAuth`
  validates by calling Sinas's `/auth/me` (Python SDK,
  `sinas/integrations/fastapi.py:152`) and recovers the originating user.
  Grove then applies that user's permissions normally. So as long as the
  token is alive, attribution flows through.
- **Agent-to-agent fan-out** doesn't propagate the token. Internal agent
  invocations stay in the Sinas backend process; each downstream tool /
  sub-agent invocation goes through `execute_function`, which mints a fresh
  per-execution token. TTL never compounds across an agent chain.

So we don't need a new JWT scope, an audience-based denylist, or a
refreshable job-token endpoint. We need two small things:

1. A public enqueue API so apps can start user-attributed jobs.
2. Per-execution tokens that live long enough to outlast the execution.

## 2. Decision

### 2.1 Public function-enqueue endpoint

The Sinas runtime API already exposes
`POST /functions/{ns}/{name}/execute/async` (fire-and-forget, returns
`execution_id`). This ADR **extends** that endpoint rather than
introducing a new path — same surface, just with optional
`callback_url`, `trigger_id`, and `delay_seconds` body fields.

```http
POST /functions/{namespace}/{name}/execute/async
Authorization: Bearer <user-access-token>     ← caller is the user, period
Content-Type: application/json

{
  "input": { ... },
  "trigger_id": "grove:run:01HX...",      // app-supplied correlation id (optional)
  "chat_id": null,                        // optional
  "delay_seconds": null,                  // optional
  "callback_url": "https://grove.example.com/sinas/callbacks/01HX..."  // optional (see §2.3)
}

→ 202 Accepted
{
  "execution_id": "exec_01HX...",
  "status": "queued"
}
```

Auth contract: the bearer's `sub` *is* the job's user. No body-level
`user_id` override, no "act as user" mode — even for service tokens.
Every queued job is attributable to the human (or human-equivalent
account) whose bearer initiated it. Audit log integrity hinges on this.

Permission: reuses the existing
`sinas.functions.execute:own` (own functions) /
`sinas.functions.execute:all` (any function) checks. **No new
`enqueue`-specific permission** — anything the caller can execute
synchronously, they can enqueue. Otherwise the queue would be a tighter
gate than the sync endpoint while offering strictly less impact (queue
serializes; sync hammers). Adding a permission for `enqueue` would
also be a footgun: admins would have to remember to grant both for
every new role.

`trigger_type` is hard-coded to `"external"` by this endpoint — it's
how we distinguish app-initiated runs from agent / webhook / schedule
triggers in the audit log.

The existing `queue_service.enqueue_function` is unchanged; this endpoint
is a thin wrapper around it.

### 2.2 Per-execution token outlives the execution

In `execution_engine.execute_function`, replace:

```python
access_token = create_access_token(user_id, user_email)
```

with:

```python
ttl = timedelta(seconds=min(
    function.timeout + EXECUTION_TOKEN_BUFFER_SECONDS,
    MAX_EXECUTION_TOKEN_SECONDS,
))
access_token = create_access_token(user_id, user_email, expires_delta=ttl)
```

- `EXECUTION_TOKEN_BUFFER_SECONDS = 300` (5 min, so downstream calls after
  the function returns still have headroom).
- `MAX_EXECUTION_TOKEN_SECONDS = 86400` (24h hard cap — anything beyond is
  almost certainly a bug).
- `create_access_token` already supports `expires_delta`; no schema change.

For most functions (`function.timeout` defaults to 30s), the resulting
TTL is ~5.5 min, *shorter* than today's 15 min default — already a
small security tightening. For functions explicitly configured with
long `timeout` (e.g. Grove's ingestion at 1800s), the token now lives
the duration of the job + buffer.

### 2.3 Optional `callback_url` for completion notification

If the request includes `callback_url`, Sinas fires a single HTTP POST
to that URL when the execution terminates (success, failure, or
timeout). Fire-and-forget — Sinas does not retry on failure.

```http
POST <callback_url>
Authorization: Bearer <freshly-minted access token for the originating user>
Content-Type: application/json

{
  "execution_id": "exec_01HX...",
  "trigger_id": "grove:run:01HX...",       // echoed from enqueue
  "function": { "namespace": "grove", "name": "ingest_document" },
  "status": "success" | "failure" | "timeout",
  "started_at": "2026-05-14T13:00:00Z",
  "finished_at": "2026-05-14T13:08:23Z",
  "duration_ms": 503000,
  "result": { ... } | null,                // present on success
  "error": "..." | null                     // present on failure / timeout
}
```

Authentication: the callback carries a fresh access token for the
originating user, same shape as the per-execution token the function
held. Apps can validate via the same `SinasAuth` path they already use
for inbound calls from functions — no new auth surface.

Delivery semantics:

- **Fire-and-forget.** One POST attempt with a 10s timeout. No retry, no
  DLQ on the callback path itself — if the app misses it, polling via
  `status_url` is the fallback.
- **No backpressure.** Sinas does not wait for the callback to succeed
  before marking the execution complete.
- **SSRF guard.** Callback URLs must be `https://` and resolve to a
  non-private address (block RFC1918 / loopback / link-local).
- **Host policy** via `CALLBACK_URL_HOSTS` env var with three states:

  | Value | Meaning |
  |---|---|
  | unset / empty | Callbacks **disabled**. Any `callback_url` in the request returns `400`. |
  | `*` | Permissive — any HTTPS non-private URL accepted. |
  | comma-separated host list (e.g. `grove.example.com,reports.example.com`) | Exact-host allowlist. |

  Default is "unset" — callbacks off until explicitly enabled. Managed-service
  deployers can validate / verify ownership of a domain by whatever process
  they like before adding it to the env var. Self-hosted single-tenant
  operators flip to `*` once.

  Why this matters: function-authoring requires admin-tier permissions;
  executing a function requires only `:execute:own`. Without this policy,
  any execute-only user can direct Sinas to POST to an arbitrary URL — a
  capability they couldn't reach through function code (which they can't
  write). The token leaked is their own, so credential theft is moot, but
  using Sinas as a DDoS source or to bypass a target's IP allowlist *is*
  newly available.

### 2.4 Batches — submit N at once, poll one aggregate

Apps doing bulk work (Grove ingesting 50 documents, an agent running over 100
dossiers) want to push the bookkeeping into Sinas: submit a batch, store
one batch_id, poll the batch for aggregate progress, drill into individual
executions only on failure. We surface this for both functions and agents
in the same pass — same `batches` table, same poll/cancel endpoints.

#### Data model

```sql
batches (
  id                    UUID PK
  user_id               UUID NOT NULL,
  kind                  TEXT NOT NULL,        -- 'function' | 'agent'
  target_namespace      TEXT NOT NULL,
  target_name           TEXT NOT NULL,
  total                 INTEGER NOT NULL,
  status                TEXT NOT NULL,        -- queued | running | completed | failed | partial | cancelled
  trigger_id_prefix     TEXT,
  callback_url          TEXT,                  -- per-execution callback (optional)
  batch_callback_url    TEXT,                  -- fires once when batch terminates (optional)
  started_at            TIMESTAMPTZ NOT NULL,
  finished_at           TIMESTAMPTZ
)

executions
  + batch_id            UUID NULL FK → batches.id, INDEX
```

Per-batch aggregate counts (`completed`, `failed`, `running`, etc.) are
computed on read via a grouped `SELECT count(*) ... GROUP BY status WHERE
batch_id = ?` query — cheap, indexed, no maintained counters.

`MAX_BATCH_SIZE` env var (default 1000) caps batch submissions to prevent
accidental DoS.

#### Submit — function batch

```http
POST /functions/{namespace}/{name}/execute/batch
Authorization: Bearer <user-token>

{
  "inputs": [
    {"doc_id": "1"},
    {"doc_id": "2"}
  ],
  "trigger_id_prefix": "grove:run:01HX",       // optional; "{prefix}:{i}" per child
  "delay_seconds": 0,                          // optional
  "callback_url": "https://app/cb/per-exec",   // optional, fires per child
  "batch_callback_url": "https://app/cb/batch" // optional, fires once at terminus
}

→ 202
{
  "batch_id": "batch_01HX...",
  "execution_ids": ["exec_01...", "exec_02..."],
  "total": 2,
  "status": "queued"
}
```

#### Submit — agent batch

```http
POST /agents/{namespace}/{name}/chats/batch
Authorization: Bearer <user-token>

{
  "inputs": [
    { "input_variables": {"company": "Acme"}, "message": "Synthesize..." },
    { "input_variables": {"company": "Beta"}, "message": "Synthesize..." }
  ],
  "trigger_id_prefix": "grove:syn:01HX",
  "callback_url": "https://app/cb/per-exec",
  "batch_callback_url": "https://app/cb/batch"
}

→ 202
{
  "batch_id": "batch_01HX...",
  "execution_ids": [...],
  "chat_ids": [...],
  "total": 2,
  "status": "queued"
}
```

#### Poll

```http
GET /batches/{batch_id}

→ 200
{
  "batch_id": "...",
  "kind": "function",
  "target": {"namespace": "grove", "name": "ingest_document"},
  "user_id": "...",
  "total": 50,
  "completed": 32,
  "failed": 1,
  "running": 5,
  "queued": 12,
  "cancelled": 0,
  "status": "running",                         // queued | running | completed | failed | partial | cancelled
  "started_at": "...",
  "finished_at": null,
  "trigger_id_prefix": "grove:run:01HX"
}
```

Terminal statuses:
- `completed` — all children completed successfully
- `partial`  — all children terminal, but ≥1 failed
- `failed`   — all children failed
- `cancelled`— batch was cancelled before all children terminated

#### Drill in

```http
GET /batches/{batch_id}/executions?status=failed&limit=50

→ 200
{ "executions": [{"execution_id": "...", "status": "failed", "error": "...", ...}, ...] }
```

#### Cancel

```http
POST /batches/{batch_id}/cancel

→ 200
{ "batch_id": "...", "status": "cancelled", "cancelled_children": 12 }
```

Marks every queued child `CANCELLED`. Running children complete (arq has
no clean preempt); their results still count toward the batch.

#### Batch callback

When the last child terminates, Sinas POSTs once to `batch_callback_url`:

```json
{
  "batch_id": "...",
  "kind": "function",
  "target": {"namespace": "grove", "name": "ingest_document"},
  "status": "partial",
  "total": 50,
  "completed": 49,
  "failed": 1,
  "started_at": "...",
  "finished_at": "...",
  "trigger_id_prefix": "grove:run:01HX"
}
```

Same auth / SSRF / fire-and-forget semantics as the per-execution callback.
Per-execution callbacks (`callback_url`) and the batch callback are
independent — both can be set, both fire.

#### Agent-batch specifics

- **Per-child callback shape** mirrors the function one but the `result`
  field has agent semantics:
  ```json
  "result": {
    "chat_id": "01HX...",
    "final_message": "the assistant's last message text",
    "final_message_role": "assistant",
    "tool_calls": [...]
  }
  ```
  Full transcript fetchable via `GET /chats/{chat_id}`.
- **Approval policy.** If an agent invokes a tool with
  `requiresApproval: true` mid-batch, the execution is marked `FAILED`
  with `error: "approval required; agent invoked async cannot pause"`.
  Apps that want bulk should run agents whose enabled tools don't
  require approval. (Future work: an `auto_approve_tools: [list]` knob
  on the batch submission.)
- **Cost.** No per-batch budget control in v1. The `MAX_BATCH_SIZE`
  cap is the primary defense; operators can audit costs via existing
  execution logs.

#### Completion detection

After every execution reaches a terminal status (`COMPLETED`, `FAILED`,
`CANCELLED`), the executor calls `batch_service.on_execution_terminated`:

```python
async def on_execution_terminated(batch_id, db):
    pending = await count_pending_in_batch(batch_id, db)  # status in (QUEUED, RUNNING)
    if pending > 0:
        return  # still running
    # All terminal — compute final aggregate and fire batch callback.
    summary = await aggregate_status(batch_id, db)
    await mark_batch_terminal(batch_id, summary)
    if batch.batch_callback_url:
        await _fire_batch_callback(batch, summary)
```

Race-safe via `UPDATE batches SET finished_at = NOW() WHERE id = ? AND finished_at IS NULL` —
only one terminator gets to fire the callback.

### 2.5 Nothing else

Specifically, **not** in this ADR:

- No new JWT scope or audience claim.
- No `jobs` table for tokens, no Redis denylist, no `/auth/jobs/mint|refresh|complete`.
- No change to how agent or function tokens are minted for in-Sinas fan-out.
- No automatic per-execution permission narrowing (the token still
  resolves the user's role permissions on each call, same as today).
- No callback retry / dead-letter queue. Polling `status_url` (or the
  batch GET endpoint) is the resilience story; callbacks are a convenience.
- No mixed-function or mixed-agent batches (single target per batch).
- No batch-level cost ceiling beyond `MAX_BATCH_SIZE`.

## 3. Impact

| Component | Change |
|---|---|
| `api/runtime/endpoints/functions.py` | Extend existing `/execute/async` with `trigger_id`, `delay_seconds`, `callback_url`. Add new `POST /functions/{ns}/{name}/execute/batch`. |
| `api/runtime/endpoints/chats.py` (or similar) | Add new `POST /agents/{ns}/{name}/chats/batch`. |
| `api/runtime/endpoints/batches.py` (new) | `GET /batches/{id}`, `GET /batches/{id}/executions`, `POST /batches/{id}/cancel`. |
| `services/execution_engine.py` | (a) Token TTL bounded by `function.timeout + buffer`. (b) Fire per-execution callback after completion. (c) Call `batch_service.on_execution_terminated(batch_id)` whenever the execution row hits a terminal state. |
| `services/batch_service.py` (new) | submit_function_batch / submit_agent_batch / get_batch_status / list_batch_executions / cancel_batch / on_execution_terminated. ~250 lines. |
| `models/batch.py` (new) + `models/execution.py` | Batch model. Add `executions.batch_id` nullable FK with index. |
| `alembic/versions/` | Two migrations: `c1a2l3b4k5s6_execution_callback_status` (already landed) and new `b1a2t3c4h5e6_batches_and_link` (creates batches + adds executions.batch_id). |
| `core/auth.py` | None (existing `expires_delta` parameter is used). |
| `core/config.py` | Add `execution_token_buffer_seconds`, `max_execution_token_seconds`, `callback_url_hosts`, `max_batch_size` (default 1000). |
| `services/queue_service.py` | Add `callback_url` + `batch_id` to function- and agent-job payloads. |
| Python SDK | `client.functions.enqueue(...)` + `client.functions.submit_batch(...)` + `client.agents.submit_batch(...)` + `client.batches.{get,list_executions,cancel}`. |
| JS SDK | `enqueueFunction(...)` (landed) + `submitFunctionBatch(...)` + `submitAgentBatch(...)` + `client.batches.{get,listExecutions,cancel}`. |
| **`docs-mint/`** | Extend `platform/async-jobs.mdx` with the batch section + function/agent side-by-side examples. |
| Grove | Migrate one worker to `client.functions.submit_batch(...)` for ingestion runs. |

Backward compatibility: full. The endpoint is net-new. The TTL change is
internal — callers that hold today's 15-min tokens still see them honored;
new executions just issue tokens scoped to their function's `timeout`.

## 4. API sketch (worked example)

Grove ingests 50 documents for user U:

```python
# Inside Grove's ingestion-trigger handler, holding the user's bearer
sinas = SinasClient(base_url=..., access_token=user_bearer)
for doc in docs:
    execution_id = sinas.functions.enqueue(
        namespace="grove",
        name="ingest_document",
        input={"doc_id": doc.id},
        trigger_id=f"grove:run:{run.id}",
        callback_url=f"https://grove.example.com/api/v1/sinas/callbacks/{run.id}",
    )
    db.add(Pending(run_id=run.id, execution_id=execution_id))
```

Each enqueued execution:

1. Sits in Redis (`sinas:queue:functions`) until a Sinas queue-worker
   dequeues it.
2. Worker calls `execute_function(user_id=U, ...)`. Mints a token with
   `expires_delta = function.timeout + 300s`. For `grove/ingest_document`
   with `timeout: 600s`, that's a 15-min token.
3. Function runs. Calls Grove's `/api/v1/documents/{id}/mark-processed`
   with the token. Grove's `SinasAuth` calls Sinas `/auth/me`, gets back
   user U, applies U's permissions. Insert succeeds against U's audit row.
4. Function returns. Token expires within minutes. No persisted credential.
5. Sinas mints a *new* short-lived token for U and fires the callback
   POST to Grove's callback URL with the execution result. Grove's
   handler validates via `SinasAuth` (same path as #3), looks up the
   `Pending` row by `execution_id`, marks the run progress. Fire and
   forget — if the callback fails to deliver, Grove can still poll
   `/api/v1/executions/{id}/status` to reconcile.

If `ingest_document` itself triggers a Sinas agent (e.g. `client.chats.invoke(...)`),
the agent runs in-process under user U with its own freshly-minted
token at each tool call — token chain never reaches Grove from inside
the agent loop, so the function token's TTL is irrelevant to the
agent's lifetime. Agent-to-sub-agent fan-out is also fine for the
same reason.

## 5. Decisions taken

Recording calls already made on this design so they don't get re-litigated:

- **No `user_id` spoofing, ever.** Bearer's `sub` is the job's user, full
  stop. No "act as user" mode even for service tokens. Audit log integrity
  outranks delegation convenience; if we ever need delegation, that's a
  separate feature with its own consent surface.
- **No new permission for enqueue.** Reuses
  `sinas.functions.execute:own` / `:all`. Anything a user can execute
  synchronously they can enqueue; the queue is a safer path than the sync
  endpoint, not a more powerful one.
- **No rate limiting in v1.** Trivial to add later if a workload abuses it.
- **Callbacks are fire-and-forget** with `status_url` polling as the
  resilience fallback. No retry, no DLQ on the callback path.
- **Agent enqueue deferred.** Functions can already invoke agents via
  `client.chats.invoke(...)`. Until a use case demands true async chat
  sessions initiated from outside Sinas, function-only is enough.

## 6. Open questions

Genuinely open, want your input:

1. **Callback failure visibility.** Should the executor record callback
   delivery status (HTTP code, duration) on the `Execution` row so
   operators can see "callback fired but the app returned 502"? Cheap to
   add; useful for debugging integrations. Lean: yes, single
   `callback_status` enum column.

## 7. When to revisit job-scoped tokens

Three concrete signals would justify reviving the larger
audience/denylist/refresh design:

1. **A pattern emerges where a function legitimately runs > 24h** (e.g.
   long-running data pipelines). We hit the hard cap and need refreshable
   tokens.
2. **An app wants to accept Sinas tokens from one set of apps but reject
   others.** Today every app that trusts the instance accepts every token
   it issues, which is fine for Sinas-first deployments but won't scale to
   multi-tenant or marketplace-style integrations.
3. **A leaked-token incident** where we needed sub-minute revocation and
   couldn't get it from "wait for `exp`." TTL ≤ 30 min plus admin "rotate
   signing key" is our current recourse.

Until one of those bites, the simpler design wins.

## 8. Next steps

1. Land the endpoint behind a feature flag (`FEATURE_FUNCTION_ENQUEUE_ENABLED`)
   in case we want to roll back fast.
2. Apply the TTL change unconditionally — it's a strict bound improvement
   regardless of the endpoint.
3. Wire the callback path (executor → POST + record `callback_status`).
4. Add the Python SDK + JS SDK helpers (`client.functions.enqueue(...)`).
5. Write the docs-mint page: endpoint surface, request/response schemas,
   callback schema, end-to-end Grove example. Land it under
   `docs-mint/build-resources/` (or `platform/` — whichever fits the
   existing IA better).
6. Pilot in Grove: convert *one* worker (suggest: discovery) to the
   user-bearer enqueue path with `callback_url` set. Audit-log a week,
   confirm attribution is correct end-to-end.
7. Convert remaining Grove workers. Mark `SINAS_API_KEY` for runtime
   callbacks as deprecated in our internal apps (keep it for genuinely
   service-level calls).
