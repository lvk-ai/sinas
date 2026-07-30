# ADR: Ephemeral sandbox execution with a baked package image

- **Status:** Accepted (runtime-validated on compose 2026-07-13: warm image build, function execution, agent codeExecution, no leftover containers)
- **Date:** 2026-06-17
- **Authors:** Kjeld Oostra
- **Related code:**
  - `app/services/container_pool.py` (the pool being replaced for the sandbox role)
  - `app/services/executor/` (the seam this plugs into — see the 3a contract)
  - `app/services/execution_engine.py` (routes untrusted execution to the sandbox executor)
  - `app/services/code_execution.py` (agent `codeExecution`, currently bypasses the seam)
  - `app/models/dependency.py` (the approved-package source of truth)
  - `app/api/v1/endpoints/dependencies.py`, `app/services/config_apply/resources.py` (dependency mutation points)
  - `app/api/v1/endpoints/containers.py`, `app/api/v1/endpoints/workers.py` (reload triggers)
  - `Dockerfile.executor` (the sandbox base image)

## Context

Untrusted function code and agent `codeExecution` run in a **warm pool of reusable
sandbox containers** (`container_pool`). Two problems with the pool, on what is and
will remain the **primary deployment (docker-compose)**:

1. **Container-reuse leak.** A pooled container is reused across up to
   `sandbox_max_executions` (100) executions. Its `/tmp`, `site-packages`, env, and
   process globals persist between runs. For untrusted, multi-tenant code this is a
   cross-execution (potentially cross-user) contamination vector. Only the `tainted`
   path discards a container; successful runs hand the same container to the next caller.

2. **Pool fragility.** Pool state is in-memory **per process**. Only the `scheduler`
   creates/replenishes containers (`scheduler/service.py` → `container_pool.initialize`);
   consumers (backend, queue-worker, queue-agent) only `_discover_existing_containers()`
   **once at startup**. If a consumer starts before the scheduler fills the pool, it
   latches at `idle=0, in_use=0` and every execution fails with
   *"No sandbox container available within 30s"*. (This is a recurring production incident.)

Packages are **dynamic**: `Dependency` rows (admin-approved pip packages) are
`pip install`-ed into each container at creation by `container_pool._install_packages`.
The pool largely exists to amortize this install cost.

## Decision

Replace the warm reuse pool **for the sandbox (untrusted) role** with **single-use
ephemeral containers backed by a pre-baked package image**:

1. **Bake packages into a sandbox image.** When the `Dependency` set changes, build an
   image `FROM` the executor base (`function_container_image`) that `pip install`s the
   approved set (and, later, any system/apt deps). Tag it by a **content hash** of the
   dependency set. Building is idempotent — if the tag already exists, skip.

2. **One container per execution.** Untrusted execution does `docker run --rm` of the
   current baked image, runs the function (unchanged in-container executor, sandbox
   mode), and tears the container down. No pool, no reuse.
   - **Leak fixed:** each execution gets a fresh container; copy-on-write means
     `site-packages` self-writes (nltk data, caches) land in *that* container's writable
     layer and die with it — isolated, no leak, and self-writing packages still work.
   - **Bug class deleted:** no shared pool ⇒ no scheduler-owns-pool, no startup
     discovery race, no replenish loop, no `idle=0` incidents.

3. **Cross-process coordination via Redis (cache, not source of truth).** The tag is a
   pure function of `hash(base_image + sorted dependency specs)` — derivable from the
   `Dependency` table, which is the real source of truth. Redis caches the
   currently-built tag (`sinas:sandbox:image_tag`) and holds a build lock
   (`sinas:sandbox:build_lock`) to avoid concurrent identical builds. On a Redis miss any
   process recomputes the tag from the deps and (re)builds — so no durability is required
   and **no new Postgres table is added**. Workers read the cached tag at execution time
   and run that image against the shared local daemon. No in-memory pool state to drift.

4. **Opt-in.** Wire as `sandbox_executor="docker_ephemeral"` (the factory branch that
   currently raises `NotImplementedError`). `docker_pool` stays the default until
   ephemeral is proven on a real compose stack; flipping the default is a later step.

5. **Route agent `codeExecution` through the seam.** `code_execution.py` currently calls
   `container_pool.acquire()` directly, bypassing the executor abstraction. Route it
   through `get_sandbox_executor()` so it benefits from ephemeral (and respects
   `sandbox_executor="disabled"`).

## Interface sketch

```python
# app/services/sandbox_image.py  (new)
def dependency_set_hash(deps) -> str: ...          # stable hash of sorted name==version
async def current_sandbox_image(db) -> str: ...     # read DB pointer (or compute+build)
async def build_sandbox_image(db) -> str:           # generate Dockerfile, docker build,
    """FROM {base}; (apt deps later); RUN pip install <deps>.
    Tag sinas-sandbox:<hash>. Update DB pointer atomically only on success.
    Keep serving the old tag if the build fails. GC superseded tags."""

# app/services/executor/docker_ephemeral_sandbox.py  (new) — SandboxExecutor
class DockerEphemeralSandboxExecutor:
    async def execute(self, *, ...timeout) -> ExecutionResult:
        image = await current_sandbox_image(db)
        # docker run --rm <image> with the SAME hardening as the pool:
        #   cap_drop ALL, no-new-privileges, pids_limit, mem/cpu limits,
        #   tmpfs /tmp, sandbox network, extra_hosts. Single use.
        ...
    async def resume(self, *, handle, resume_value, execution_id, timeout):
        # Sandbox mode can't reach input() (raises), so this never fires in
        # practice; satisfies the Protocol. (Durable HITL is issue #79.)
        ...

# factory.py: the docker_ephemeral branch returns DockerEphemeralSandboxExecutor()
```

**Build trigger** — hook the existing dependency-change paths:
`dependencies.py` (add/remove), `config_apply/resources.py` (declarative), and the
`reload_packages` endpoints (`containers.py`, `workers.py`) call `build_sandbox_image`
instead of reinstalling into pool containers.

## Impact

| Component | Change |
|---|---|
| `executor/factory.py` | Implement `docker_ephemeral` branch |
| `executor/docker_ephemeral_sandbox.py` *(new)* | The ephemeral SandboxExecutor |
| `services/sandbox_image.py` *(new)* | Hash + build + DB tag pointer + GC |
| `execution_engine.py` | **No change** — already routes via the SandboxExecutor seam (post-3a) |
| `code_execution.py` | Route agent codeExecution through `get_sandbox_executor()` |
| `dependencies.py` / `config_apply/resources.py` / reload endpoints | Trigger image build on dependency change |
| `scheduler/service.py` | Sandbox-pool init becomes a no-op in ephemeral mode |
| `container_pool.py` | Untouched; only used when `sandbox_executor="docker_pool"` |
| `Dockerfile.executor` | Unchanged — stays the base the baked image `FROM`s |
| Redis | Cache of the current built image tag + build lock. No new Postgres table — the tag derives from the `Dependency` set (the source of truth) |

## Open questions

- ~~**Tag pointer storage:**~~ **Resolved:** Redis cache + build lock; tag derived from
  the `Dependency` set (no new Postgres table).
- **Build timing:** synchronous on the admin dependency-change action (simple; admin
  waits) vs. background with status. Lean **sync for MVP** (infrequent admin op).
- **Concurrency cap:** bound concurrent ephemeral containers (semaphore, default
  `sandbox_max_size`) vs. rely on worker `max_jobs`.
- **Cold-start latency:** plain ephemeral pays container-create (~hundreds of ms) per
  execution. Acceptable for MVP? An optional **single-use warm buffer** is an orthogonal
  later optimization if a workload needs sub-100ms starts.

## What we'd NOT do (first cut)

- Not touch the trusted/shared executor (`shared_worker_manager`) — separate concern
  (the `inprocess` trusted executor, step 4).
- Not change the in-container executor or the execution wire format.
- Not make ephemeral the default — opt-in until validated on compose.
- Not build the single-use warm buffer.
- Not support await-input under ephemeral (sandbox can't await; durable HITL is #79).
- Not solve multi-host image distribution (registry push/pull) — compose is single-host.
- Not add system/apt-dep support yet (the `Dependency` model is pip-only today; baking
  leaves room for it later).

## Next steps (if accepted)

1. ✅ `sandbox_image.py`: dependency hash + `build_sandbox_image` + Redis tag cache/lock + GC + self-correcting resolver.
2. ✅ `DockerEphemeralSandboxExecutor` + factory wiring.
3. ✅ Build triggers (reload endpoint + scheduler warm-build); correctness is self-correcting regardless.
4. ✅ Route `code_execution.py` (agent codeExecution) through a shared ephemeral lifecycle (`executor/_ephemeral_runtime.py`), used by both the function executor and the code path.
5. ✅ No-op sandbox-pool init in ephemeral mode (scheduler).
6. ⬜ Docs (the `SANDBOX_EXECUTOR` env var); **runtime validation on a compose stack** (the gate before flipping the default).

Unit coverage: `tests/unit/test_executor_result.py` covers the `from_wire` wire→typed translation (the contract boundary). The container IPC paths require a live Docker daemon to exercise.
