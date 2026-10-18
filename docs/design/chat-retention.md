# Chat retention: archive, then delete

- **Status:** Accepted, implemented on `feat/chat-retention` (backend) and `feat/chat-retention-console` (console)
- **Date:** 2026-10-18
- **Authors:** Kjeld Oostra, Claude
- **Related code:**
  - `backend/app/services/retention/`: `policy.py` (settings and per-agent resolution), `archive.py` (format, build, parse, verify), `store.py` (the PutObject-only S3 writer), `job.py` (selection, archive, delete, runs), `restore.py` (the restore CLI)
  - `backend/app/api/v1/endpoints/retention.py`: `/api/v1/retention/*`
  - `backend/app/queue/worker.py`: the `chat_retention_tick_job` cron and the `chat_retention_run_job` the API queues
  - `backend/app/cli.py`: `retention` and `restore-chat`
  - `backend/app/models/retention_run.py`, migrations `r7e8t9e0n1t2` (`agents.retention_days`, `chat_retention_runs`) and `r8e9t0i1d2x3` (`ix_chats_updated_at_id`, built concurrently)
  - `backend/app/schemas/spec/agent.py`, `backend/app/services/resources/agents.py`: `retentionDays` on the agent
  - `console/src/pages/AgentDetail.tsx` (retention field), `console/src/components/RetentionPanel.tsx` (System page)
- **Related docs:** `docs-mint/admin/chat-retention.mdx`

## 1. Context

Chats and their messages stay in Postgres forever. Deployments with a data
retention obligation need old conversations out of the primary database,
but usually also need to be able to produce them later (an audit, a legal
hold, a user's access request). Hence: archive each expiring chat to object
storage that Sinas can write but never read back or remove, and only then
delete it from Postgres.

`expires_at` on a chat (set per chat by its creator, swept hourly by
`cleanup_expired_chats`) is a different thing and is left alone: it is a
per-chat hard delete the caller asked for, not a deployment policy.

## 2. Decisions

1. **Archive, then delete.** With an archive destination configured, a chat
   is written to object storage first and deleted only after the upload
   returned success. `CHAT_RETENTION_ACTION=delete` allows deleting
   without a destination; a configured destination is still written first.
   `CHAT_RETENTION_ACTION=archive` (the default) with no destination is a
   refused run: nothing is deleted.
2. **Off by default, and one switch.** Nothing happens unless the operator
   sets `CHAT_RETENTION_ENABLED=true`. `CHAT_RETENTION_DAYS` unset or `0`
   keeps chats forever unless an agent says otherwise.
3. **Policy: global default, per-agent override.** `retentionDays` on the
   agent: unset/null = the global default, `0` (or `"never"` in config) =
   keep forever, `N` = N days. It goes through the agent applier, so REST,
   config apply/export and change history all carry it.
4. **Age is last activity.** A chat's age is measured from
   `max(chats.updated_at, its newest message's created_at)`. Adding a
   message doesn't touch `chats.updated_at`, so both are needed. A chat in
   use is never cut, however old it is.
5. **Write-only archive.** Sinas needs `s3:PutObject` on
   `<bucket>/<prefix>/*` and nothing else (plus `kms:GenerateDataKey` with
   SSE-KMS). No Get, List, Head or Delete, ever: the job never checks
   whether an object exists, it writes the same key again.
6. **One gzipped JSONL object per chat**, at
   `<prefix>/<yyyy>/<mm>/<chat_id>.jsonl.gz`, `yyyy/mm` from the chat's
   `created_at` (stable, so a retry writes the same key).
7. **Restore is a CLI**, `python -m app.cli restore-chat <s3-uri|file>`,
   reading with credentials the operator provides separately (the standard
   AWS chain or `--profile`), never Sinas's archive credentials (which
   can't read anyway). `--dry-run` validates without writing.
8. **`llm_usage` stays.** No content, needed for cost reporting. Its
   `chat_id` is not a foreign key, so nothing changes for it.
9. **Admin API and permissions.** `GET /api/v1/retention/status`,
   `POST /api/v1/retention/run?dry_run=true|false`, behind
   `sinas.retention.read:all` / `sinas.retention.update:all` (Admins have
   them through `sinas.*:all`; nobody else does by default).

## 3. The job

### Scheduling

The agent worker's arq cron (where the package repository refresh lives)
runs `chat_retention_tick_job` every 10 minutes, `unique=True` so one worker
runs it. The tick starts a run when the cron expression
`CHAT_RETENTION_SCHEDULE` (default `30 3 * * *`, UTC) had a fire time since
the last scheduled run started, or when the last run, whoever started it
(schedule, Run now, CLI), stopped before the end: at its time or size budget
(`partial`) or cut off (`interrupted`). A large first backlog is worked
through in consecutive runs instead of waiting a day each time.

**Continuations pick up where the last run stopped.** Every run saves its
counts and a cursor, the last chat it looked at in selection order
`(chats.updated_at, id)`, after each batch. A run that follows a partial,
interrupted or failed one starts after that cursor; one that follows a
completed run starts from the oldest chat. Skipped and failed chats count
against `CHAT_RETENTION_MAX_CHATS_PER_RUN` (a run must end), but they can't
starve the rest: the chain walks past them, and the next fresh chain (the
next scheduled fire) retries them, which is their backoff.

**Interrupted runs.** A run the worker cancels (job timeout, shutdown)
records itself `interrupted` with its counts and cursor, then lets the
cancellation through. One that died outright (process killed) stays
`running` on record: past its window, the status shows it as interrupted,
and the next tick records it so and continues from its cursor.

"Run now" (`POST /retention/run`) records a queued run and queues
`chat_retention_run_job` on the agent queue; the HTTP request doesn't wait
for it.

### Selection

One query, keyset-paginated on `(chats.updated_at, chats.id)`, in batches of
`CHAT_RETENTION_BATCH_SIZE` (default 200):

- effective days = the agent's `retention_days` when set, else the global;
  `0` excludes the chat. A chat whose agent was hard-deleted
  (`agent_id NULL`) uses the global default.
- `chats.updated_at < now - days`, and no message with
  `created_at >= now - days` (`ix_messages_chat_id_created_at`: one probe).
- a pre-filter `chats.updated_at < now - (shortest positive policy)` that
  the new `ix_chats_updated_at_id` index serves, so a run reads only old
  chats.

Each row also gets a `blocked` reason, computed in SQL:

| Reason | Means |
|---|---|
| `parked` | a `pending_completions` row (ask_user, a suspended delegation, a waited-on background run) |
| `pending_approval` | an unanswered tool approval that hasn't expired |
| `wakeup` | a timer not yet delivered, or a watch neither cancelled nor expired |
| `mcp_task` | an MCP task not finished, or with deliveries still queued |

Then, per chat, in Redis: the chat loop lock is held (a turn is running) →
`running`; a background job of the chat is queued or running →
`background_job`. Blocked chats are counted and left alone; they come up
again in the next run.

### Per chat

1. **Claim**: `pg_try_advisory_xact_lock(<key from chat id>)` in a
   transaction on the runner's own claim connection, kept open until the
   chat is done. Not obtained → another runner has it → skipped
   (`claimed`). A transaction-level lock because session-level advisory
   locks don't survive pgbouncer's transaction pooling (an open transaction
   pins its server connection, so this one does). The claim transaction
   reads nothing after taking the lock (READ COMMITTED: no snapshot held),
   holds no row lock, blocks nothing but another claim on the same chat, and
   is released when the runner dies.
2. **Snapshot and eligibility from one read**: one REPEATABLE READ
   transaction checks the chat still qualifies under the *current* policy
   (retention on; idle past its agent's current effective days, newest
   message included; nothing blocking) and reads the chat, its messages
   (by `created_at, id`), its `tool_call_results` and its workbench file
   list. A chat that was written to after selection is skipped here
   (`not_eligible`), before anything is uploaded.
3. **Upload** (no transaction open). A failure → counted, nothing deleted,
   the chat comes up again next run. An archive whose uncompressed size
   would exceed what a restore reads back (512 MiB) is not uploaded: the
   chat is kept and counted as `too_large` ("too large to archive safely").
4. **Delete**, one short transaction: `SELECT … FOR UPDATE SKIP LOCKED` on
   the chat row (taken by anything else → skipped), then, under that lock,
   the same eligibility check again from the database (a message, a
   `retentionDays` change to never, or retention switched off since the
   upload → `not_eligible`, kept) and a check that the chat is still
   exactly what was archived (a digest of everything in the archive;
   changed → `changed`, kept, archived again next run). Then delete the
   chat-owned rows and the chat, commit, remove the workbench's stored
   blobs (best effort, as the expiry sweep does).
5. Release the claim.

A crash or failure between 3 and 4 leaves an archived chat in Postgres; the
next run archives it again under the same key (a second object version with
versioning on, which Object Lock allows) and then deletes it. Never the
other way round.

The `FOR UPDATE` on the chat row also orders the delete against a message
written at that moment: the message insert's foreign key check waits for
the row, and fails once the chat is gone, rather than landing in a deleted
chat.

### What references a chat

Checked against a migrated database, not just the models (the models claim
cascades that a later autogenerated migration dropped):

| Table | Reference | On delete | Retention does |
|---|---|---|---|
| `messages` | `chat_id` FK | NO ACTION | archived, deleted |
| `pending_tool_approvals` | `chat_id`, `message_id` FKs | NO ACTION | unanswered ones block; answered or expired ones are deleted first (not archived: the transcript holds the outcome) |
| `pending_completions` | `chat_id` FK | NO ACTION | block (none left to delete) |
| `executions` | `chat_id` FK | NO ACTION | kept, `chat_id` set NULL: function execution records have their own lifecycle |
| `chat_wakeups` | `chat_id` FK | CASCADE | active ones block; spent ones deleted (not archived: a watch's token is a credential) |
| `mcp_tasks` | `chat_id` FK | CASCADE | unfinished ones block; finished ones deleted (not archived: a server task id can be a bearer token) |
| `tool_call_results` | `chat_id`, no FK | — | archived, deleted |
| workbench `collections` | `namespace='_chat', name=<chat id>`, no FK | — | **not archived** (listed by path, size and hash in the archive); deleted with the chat, blobs removed |
| `llm_usage` | `chat_id`, `message_id`, no FK | — | **left alone** |

Deleted explicitly, children first, so the job doesn't depend on which
`ON DELETE` rules a given database ended up with.

## 4. Archive format

gzip, UTF-8 JSON lines, `\n`-terminated. Line 1:

```json
{"type": "chat", "format": "sinas.chat-archive", "format_version": 1,
 "sinas_version": "0.4.0", "archived_at": "…",
 "retention": {"days": 90, "source": "agent", "action": "archive",
               "last_activity_at": "…"},
 "chat": {"id": "…", "user_id": "…", "user_email": "…", "agent_id": "…",
          "agent_namespace": "support", "agent_name": "triage", "title": "…",
          "archived": false, "created_at": "…", "updated_at": "…",
          "expires_at": null, "session_key": null, "job_timeout": null,
          "keep_alive": null, "chat_metadata": {}},
 "workbench": {"files": [{"path": "notes.md", "content_type": "text/markdown",
                          "versions": [{"version": 1, "size_bytes": 120, "sha256": "…"}]}]}}
```

Then one line per message (`created_at, id` order):

```json
{"type": "message", "id": "…", "role": "assistant", "content": "…",
 "tool_calls": [...], "tool_call_id": null, "name": null, "created_at": "…"}
```

then one per stored tool result (`type: "tool_result"`: id, tool_call_id,
tool_name, arguments, result, result_size, status_code, duration_ms, source,
created_at, expires_at), and last a manifest:

```json
{"type": "manifest", "counts": {"message": 12, "tool_result": 3},
 "sha256": "<hex sha256 of every byte before this line, uncompressed>"}
```

The upload sends `Content-MD5` (S3 requires it for writes to an Object Lock
bucket, and it's integrity in transit), `Content-Type:
application/x-ndjson`, `Content-Encoding` is *not* set (the object is a
`.gz` file, not a transparently compressed one), and metadata
`sinas-chat-id`, `sinas-format-version`, `sinas-sha256` (of the gzip bytes).

Never in an archive: `active_channel_id` (a stream channel id), wakeup
tokens, MCP server task ids, API key ids, approval conversation contexts,
any credential. Message content is whatever the conversation held: if a user
pasted a secret into a chat, it is in the transcript and so in the archive,
exactly as it was in Postgres.

## 5. Restore

`python -m app.cli restore-chat <s3://bucket/key | path> [--dry-run]
[--endpoint-url URL] [--region R] [--profile P] [--user-email E]`

- Reads with the standard AWS chain (`AWS_ACCESS_KEY_ID`/`--profile`/…), so
  the reader is whoever the operator chooses, not Sinas.
- Verifies the format version, the manifest's counts and sha256, every
  line's shape, unique ids. Any failure → exit 1, nothing written.
- `--dry-run` stops there and prints what would be restored and what would
  conflict (chat id taken, a message id taken, user missing, agent missing).
- Otherwise one transaction: the chat under its original id and the
  messages under theirs. The owner is the archived `user_id` if it exists,
  else the user with the archived email, else `--user-email`; none → refused.
  The agent link is resolved by namespace/name (missing → unlinked, the
  namespace/name kept). Tool results are restored if their `expires_at` is
  still ahead (older ones would be dropped with their partition anyway).
- The restored chat's `updated_at` is now: it gets a full retention period
  again instead of being archived and deleted by the next run.

## 6. Judgement calls

- **boto3**, called through `asyncio.to_thread`, not a hand-rolled SigV4
  client over httpx. Instance roles were a requirement: the credential chain
  (web identity/IRSA, ECS and EKS pod identity, IMDSv2, profiles) is the
  part not worth re-implementing. aiobotocore pins botocore narrowly and
  pulls in aiohttp; one PutObject per chat in a nightly job doesn't need
  async I/O. Adds boto3, botocore, s3transfer to the lock.
  `request_checksum_calculation="when_required"` keeps boto3 ≥ 1.36 from
  sending the CRC32 trailers GCS's interop API and older S3-compatibles
  reject.
- **An operator switch** (`CHAT_RETENTION_ENABLED`) on top of the days
  settings, so that an agent's `retentionDays` alone never deletes anything
  on a deployment that hasn't opted in.
- **Changing an agent's `retentionDays` over REST takes
  `sinas.retention.update:all`.** An agent owner could otherwise shorten
  retention for every user's chats with their agent. Config apply and
  package install are admin-level already.
- **REST: an explicit `retention_days: null` clears the override** (back to
  the default). Every other agent field treats null as "unchanged"; this one
  needs a way back to "use the default", and the console sends the field
  only with its form value.
- **An audit table, not a change-history revision.** `chat_retention_runs`
  holds one row per real run (trigger, actor, policy snapshot, counts per
  outcome and per agent, error). `config_revisions` is resource history with
  restore semantics; a run isn't a resource. Dry runs aren't recorded: they
  change nothing.
- **Workbench files are deleted, not archived** (v1). Archiving them would
  need either reading blobs into the archive (size) or a second object per
  file; the archive lists them so it's visible what went.
- **`keep_alive` alone doesn't protect a chat.** It's a stream setting; a
  keep-alive chat idle for the retention period is as old as any other.
  What protects a chat is something that will write to it: a running turn,
  a parked round, a pending approval, a live wakeup or MCP task, a
  background job.
- **Executions are unlinked, not deleted.** Their input/output can hold tool
  arguments and results; they are the function execution log, with their
  own consumers (Functions › Executions). Documented as not covered.
- **Budgets.** `CHAT_RETENTION_MAX_CHATS_PER_RUN` (default 0, no limit) and
  `CHAT_RETENTION_MAX_RUN_SECONDS` (default 3300) end a run as `partial`;
  the next tick continues after its cursor (§3).
- **Oversized chats are kept, not archived.** The writer applies the
  reader's 512 MiB cap rather than the reader streaming without one: an
  archive nobody can restore must never be the reason a chat is deleted.
- **`retentionDays` is capped at 36500** (100 years), in the spec, the REST
  schemas and a CHECK constraint, the same cap as the console's input.
- **Restore and session keys.** One active chat per (agent, session key):
  if the agent's session continues in a newer chat, the restore detaches
  the restored chat from the session (restores it without its key) and
  says so, rather than failing the insert. `--detach-session` does that on
  request. An empty or null title restores as an empty title.

## 7. Out of scope (v1)

- Archiving workbench files.
- Restore from the console or API.
- Per-user retention, legal holds (a hold is `retentionDays: 0` on the
  agent, or switching retention off).
- ClickHouse request logs: their TTL is `CLICKHOUSE_RETENTION_DAYS`
  (default 90), applied by the existing ClickHouse storage migration.
