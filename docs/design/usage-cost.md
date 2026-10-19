# LLM cost tracking

- **Status:** Accepted, implemented on `feat/usage-cost`
- **Date:** 2026-10-14
- **Authors:** Kjeld Oostra, Claude
- **Related code:**
  - `backend/app/services/llm_pricing.py`: ratecard parsing, matching, cost, the deployment ratecard, URL refresh, backfill
  - `backend/app/services/usage_reports.py`, `backend/app/api/v1/endpoints/usage.py`: `/api/v1/usage/*`
  - `backend/app/providers/tracking.py`, `backend/app/scheduler/jobs/poll_provider_batches.py`: where rows are priced
  - `backend/app/schemas/spec/llm_provider.py`, `backend/app/services/resources/llm_providers.py`: provider pricing
  - `backend/app/models/llm_usage.py`, `backend/app/models/ratecard_cache.py`, migrations `c1o2s3t4u5s6` (columns, table), `c2u3s4i5d6x7` (indexes), `c3p4r5f6a7m8` (`pricing_provider`) and `b4f5i6l7l8b9` (marks existing batch rows)
  - `console/src/pages/Usage.tsx`, `console/src/pages/LLMProviders.tsx`, `console/src/components/Ratecard.tsx`, `console/src/lib/ratecard.ts`: Admin › Usage, the provider form's Pricing section and the providers' pricing overview
- **Related docs:** `docs-mint/admin/usage-cost.mdx`

## 1. Context

`llm_usage` has one row per LLM call: attribution (user, chat, message,
agent, source), provider and model, prompt/completion/total tokens and the
cache-read and cache-write portions, latency, errors. It had no cost, and
the only read API was token sums in `GET /messages/stats`. An external app
read the table over SQL to enforce a per-run cost cap.

The goal: a cost on every call, an API broken down by agent, provider,
model, user and more, and a cheap way to get the cost of a set of chats so
that app can stop reading the table.

## 2. Decisions

1. **LiteLLM's ratecard format.** `model_prices_and_context_window.json`,
   so operators can point at LiteLLM's maintained file. Only
   `input_cost_per_token`, `output_cost_per_token`,
   `cache_read_input_token_cost`, `cache_creation_input_token_cost`,
   `input_cost_per_token_batches`, `output_cost_per_token_batches` and
   `litellm_provider` are read; everything else is ignored. An entry that
   prices neither input nor output (image, audio, search models) is skipped,
   as is `sample_spec`. Negative, non-finite or non-numeric prices count as
   missing.

2. **Two sources, provider first.**
   1. The provider's own `pricing` (model → the same fields), after its
      `modelAliases`, plus a `discount` multiplier.
   2. The deployment ratecard: `MODEL_PRICES_JSON`, `MODEL_PRICES_FILE` and
      `MODEL_PRICES_URL`.

   No bundled defaults: stale prices are worse than none. A call nobody
   priced gets `cost_usd NULL` and counts as unpriced.

3. **Cost frozen at write time.** `cost_usd` (Numeric(20,10)),
   `price_source` (`provider` | `deployment` | `backfill`) and `batch` on
   `llm_usage`. A later price change never rewrites a priced row. An admin
   endpoint prices rows whose cost is NULL with the current ratecard.

4. **API under `/api/v1/usage`** with `sinas.usage.read:all` /
   `sinas.usage.read:own`, the latter enforced server-side by AND-ing
   `user_id = caller` into every query.

## 3. Judgement calls

### Ratecard

- **The three deployment sources merge** rather than one excluding the
  others: inline JSON over file over URL, per model. The common case is
  LiteLLM's file by URL plus a handful of overrides inline.
- **The URL copy lives in `ratecard_cache`**, reduced to the fields used
  (LiteLLM's file is mostly context windows and capability flags). A failed
  fetch records `last_error` and keeps the entries. The entries are only
  used while `url` matches `MODEL_PRICES_URL`; pointed elsewhere, the old
  copy is ignored (it describes another source) until the new one is
  fetched. An empty or price-less ratecard counts as a failed fetch.
- **Refresh cadence.** The agent worker's cron checks every 10 minutes and
  fetches only when the copy is missing, from another URL, or older than
  `MODEL_PRICES_REFRESH_HOURS` (24). That keeps the first fetch after a
  deploy within 10 minutes, and retries a failing URL every 10 minutes
  without refetching a good one. `POST /usage/ratecard/refresh` forces it.
- **Fetch hardening** reuses the package repository fetcher (`_get`, given
  explicit bounds): https on every redirect hop, a size cap
  (`MODEL_PRICES_MAX_BYTES`, 16 MB — LiteLLM's file is a few MB), and a
  timeout. Plain http is refused outright, with no development override.
- **Each process caches the merged ratecard and each provider's pricing
  for a minute.** The file layer is re-read when its mtime changes, the URL
  layer when the stored copy's `fetched_at` changes. A provider pricing edit
  applies to new calls within a minute. So the insert of a usage row
  usually makes no pricing query at all; when a cache is due, its reads run
  in a savepoint of the insert's transaction, so a failed or timed-out read
  leaves that row (and the batch poller's other writes) committed, unpriced.
- **Reloads are serialised** (one asyncio lock per process) and publish a
  layer only once it is fully read: concurrent calls keep pricing against
  the previous ratecard, never a half-loaded one, and a failed reload stays
  due.
- **The cache row is upserted** (`INSERT … ON CONFLICT DO NOTHING`, then a
  row lock), so the first refresh by an admin and by the cron can overlap.

### Matching

For model `M` through a provider of type `T` (mapped to LiteLLM's provider
name: `azure_openai` → `azure`, otherwise the same), or the provider's
`pricingProvider` `P` when set:

1. `T/M` (or `P/M`), then bare `M`.
2. Only if neither exists, `M` without a trailing release date
   (`-YYYY-MM-DD`, `-YYYYMMDD`, `@YYYYMMDD`), again `T/` (`P/`) first.

Case-insensitive. No prefix or fuzzy matching: `gpt-4o-mini` must never be
priced as `gpt-4o`. A model alias replaces the name before any lookup (in
the provider's pricing and the ratecard alike); aliases are the answer to
names that don't match, such as Azure deployment names.

### OpenAI-compatible providers (`pricingProvider`)

A provider of type `openai` pointed at Mistral, Groq, Together, DeepSeek or
a LiteLLM proxy was priced as OpenAI: its models came out unpriced, or
matched an OpenAI entry of the same name, unless every model got an alias.
`pricingProvider` (`pricing_provider`, a column on `llm_providers`) names
the LiteLLM family to look the models up under instead.

- **Explicit, never inferred from the endpoint.** Guessing the family from
  the base URL breaks with proxies, gateways and self-hosting, and would
  silently change prices when a URL changes.
- **In place of the type's prefixes, not besides them.** Set, `P/M` is
  tried and `T/M` is not; the bare-name fallback is unchanged. Trying both
  would let an OpenAI entry win for a model the operator said is Mistral's.
  It applies to the provider's own `pricing` keys too (they are normally
  bare names, so this rarely matters).
- **Validated as a lowercase identifier** (`[a-z0-9][a-z0-9_-]*`, ≤ 64),
  not against the ratecard: the ratecard is fetched and changes, and a
  config valid today must not fail tomorrow. Config apply warns when the
  deployment ratecard as loaded has no models for the family; the console
  shows the same next to the field and in the LLM Providers overview.
- **Families** (`provider_families` in `GET /usage/ratecard`, for the
  console's dropdown): each `litellm_provider` value with its model count,
  plus a key prefix that names its entry's family (`vertex_ai/claude-…` is
  listed under `vertex_ai-anthropic_models`, so `vertex_ai` counts too,
  since the prefix is what matching uses). A prefix that isn't part of the
  entry's family (LiteLLM's image-size keys such as `low/1024-x-1024/…`)
  is not a family.
- **Unset keeps today's behaviour and checksums**: it is left out of the
  canonical form, as pricing is. Calls already priced keep their cost;
  the backfill prices NULL-cost rows with the provider's current
  `pricingProvider`, like new calls.

### Discount

`discount` (0–1) multiplies **deployment-ratecard** prices for that
provider's calls: it models a negotiated percentage off list. It does not
apply to the provider's own `pricing`, which is taken as the price actually
paid; applying it there would discount twice. Values above 1 (markups) are
refused: this is a discount, not a chargeback mechanism.

### Cost arithmetic

Every provider's `extract_usage` reports `prompt_tokens` **including** cache
reads and writes (Anthropic's `input_tokens` excludes them; the provider
adds them back; OpenAI's `cached_tokens` is already part of
`prompt_tokens`). So:

```
uncached = max(prompt - cache_read - cache_write, 0)
cost = uncached*input + cache_read*cache_read_price
     + cache_write*cache_write_price + completion*output
```

- Missing cache price → the input price (an upper bound; LiteLLM omits a
  cache price mostly where there's no discount).
- A token kind the call used without a price → the whole call unpriced,
  not partially priced.
- Batch: the `_batches` prices where listed, else the regular ones (upper
  bound). Cache tokens in a batch call are scaled by the same ratio as the
  input price (Anthropic's batch discount applies to cache tokens too).
- Rounded half-up to 10 decimal places, which is what Postgres does when
  storing into `Numeric(20,10)` — the backfill's SQL and the Python agree to
  the last digit.
- Not modelled: long-context tiers (`*_above_200k_tokens`), the one-hour
  cache-write price, per-request fees, reasoning-token surcharges beyond
  what providers already count as completion tokens. Calls on tiered models
  past the tier boundary are under-priced.

### Batch calls

The batch poller (`poll_provider_batches`) wrote usage rows with
`source="provider_batch"` and no other marker. It now sets `batch=True` and
prices them in its own transaction. Migration `b4f5i6l7l8b9` marks existing
`provider_batch` rows `batch=true`, 50k rows per statement, each committed
on its own so `llm_usage` stays writable (doing it inside `c1o2s3t4u5s6`
held ACCESS EXCLUSIVE on the table for the whole rewrite). Only succeeded results are recorded, as
before. The model is the agent's model at poll time (as before), not
necessarily the one at submit time.

### Backfill

`POST /usage/backfill {from?, to?}` prices rows with `cost_usd IS NULL` in
the range at today's prices and stamps `price_source="backfill"`, so a
reader can tell they weren't priced at call time. It runs one `UPDATE` per
(provider, provider type, model, batch) group with the cost formula in SQL,
committing per group, so it never loads rows and never holds one long
transaction. A deleted provider's rows fall back to the deployment ratecard.
Without a `to`, the range ends when the backfill starts, and the counts it
returns are the rows it priced and the rows still unpriced in the range
afterwards, so calls recorded meanwhile can't skew them.

### API

- **Window defaults.** `from` defaults to 30 days before `to` (now) — except
  when `chat_id` is given on `summary`/`calls`: a chat's calls are found by
  the `chat_id` index whenever they were made, which is what a per-run cap
  needs. `timeseries` always defaults to 30 days (it has to enumerate
  buckets) and refuses more than 2000 buckets.
- **Filters repeat.** All of agent, provider, model, user_id, source and
  chat_id accept several values (`IN`); chat_id is capped at 500 per
  request to keep URLs sane.
- **read:own** is AND-ed into every query. Asking for another user's id or
  chat returns empty results rather than 403, so the same client code works
  under either scope. The `Users` default role gets `read:own` (it already
  saw its own token counts through `/messages/stats`).
- **Ratecard and admin actions** need `read:all` (the ratecard shows
  provider pricing and models across all users) and `update:all` (refresh,
  backfill) respectively.
- **group_by** takes agent, provider, model, user, source, chat, batch and
  one of day/hour, in any combination. Groups are sorted by time bucket,
  then cost and calls, with a `limit` (default 1000) and `truncated` flag.
  `user` groups carry the user's email. Day and hour buckets are UTC.
- **`cost_usd`** is a JSON number (float). It sums priced calls only;
  `unpriced_calls` says how many were left out. `/messages/stats` gains
  `llm_cost_usd` and `llm_unpriced_calls` from the same aggregate it
  already ran.

### Indexes

`llm_usage` already had `(user_id, created_at)`, `(created_at)` and
`(chat_id)`. Added `(agent, created_at)` and `(provider_name, created_at)`,
built `CONCURRENTLY` so the per-call inserts aren't blocked, in a revision
of their own: a failed concurrent build then never strands the columns
migration half-applied, and rerunning it drops any INVALID leftover
first. On 2M rows
spread over 180 days (≈11k/day), EXPLAIN ANALYZE showed:

| Query (30-day window unless noted) | Plan | Time |
|---|---|---|
| totals, no filter (≈333k rows) | parallel index scan on `idx_llm_usage_created` | 346 ms |
| agent filter, group by model | bitmap scan on `idx_llm_usage_agent_created` | 7 ms |
| user filter (read:own) | bitmap scan on `idx_llm_usage_user_created` | 2 ms |
| 20 chat_ids, no lower bound | index scan on `ix_llm_usage_chat_id` | 0.2 ms |
| calls page, agent filter | backward scan on `idx_llm_usage_agent_created` | 0.4 ms |
| provider filter (1 of 5 providers, 20% of rows), group by day | index scan on `idx_llm_usage_created` + filter | 255 ms |

With a provider holding a fifth of all rows, the planner rightly prefers the
time index; the provider index pays off for providers with a small share.
Unfiltered windows scale with the rows in the window; no covering index was
added for them (the console asks for them once per page load).

### Provider pricing as a resource field

`pricing`, `model_aliases`, `pricing_discount` and `pricing_provider` are
columns on `llm_providers` and fields of `LLMProviderSpec` (`pricing`,
`modelAliases`, `discount`, `pricingProvider` in YAML; `pricing`,
`model_aliases`, `discount`, `pricing_provider` over REST), so
config apply, export, change history and restores carry them like any
other field. Unset, they are left out of the canonical form: providers
applied before pricing existed keep their checksum and show no change.
Model entries forbid unknown fields (a typo such as
`input_cost_per_tokens` is an error, not a silently free model) and need
an input or output price. Config is declarative as for every other field:
a YAML that leaves `pricing` out clears it.

## 4. Not done

- Budgets or caps enforced inside Sinas (the API makes it cheap to enforce
  them outside).
- Currencies other than USD.
- Repricing priced rows after a price correction (deliberately: history is
  frozen; an operator who must correct it can set `cost_usd` to NULL in SQL
  and backfill).
