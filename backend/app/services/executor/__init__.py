"""Pluggable execution backends for function code.

Two protocols, two security postures, one dispatch surface:

- `SandboxExecutor` — runs untrusted code (per-function untrusted functions
  and agent `codeExecution`). Implementations must isolate per execution
  and prevent cross-user leakage of state, files, or env.
- `TrustedExecutor` — runs admin-approved code (functions explicitly
  flagged with `shared_pool=True`). No per-execution isolation guarantee;
  performance is the priority.

The choice between the two is made per function call by
`execution_engine.execute_function` based on `Function.shared_pool`.
The choice of which *implementation* to use for each role is a deploy
setting (`settings.sandbox_executor`, `settings.trusted_executor`) and
is resolved by `executor.factory.get_sandbox_executor` /
`get_trusted_executor`.
"""

from app.services.executor.base import SandboxExecutor, TrustedExecutor
from app.services.executor.factory import (
    get_sandbox_executor,
    get_trusted_executor,
)

__all__ = [
    "SandboxExecutor",
    "TrustedExecutor",
    "get_sandbox_executor",
    "get_trusted_executor",
]
