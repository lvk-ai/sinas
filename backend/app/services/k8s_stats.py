"""Kubernetes-side data for the System page.

On a Kubernetes deployment there is no Docker socket, so the System page's
service list, per-service CPU/memory and the sandbox / shared container cards
come from the Kubernetes API instead:

- pods of the release (label `app.kubernetes.io/instance=<release>`), mapped
  to Sinas components by the chart's `app.kubernetes.io/component` label and,
  for executor pods, `sinas.type`;
- CPU/memory from `metrics.k8s.io` when metrics-server is installed. Without
  it (or without RBAC to read it) pods still show phase / ready / restarts /
  age, and the caller gets a human-readable reason instead of numbers.

Credentials are the pod's own ServiceAccount (the chart's executor-manager
SA, plus the `systemStats.rbac` Role for metrics). Every failure is turned
into a `K8sUnavailableError` carrying a reason the console can show — this module
never raises anything else to its callers.

The kubernetes client is blocking; the async helpers run it in a thread with
a short request timeout so a slow apiserver cannot hang the page.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

_API_TIMEOUT = 5  # seconds per apiserver request
_MIB = 1024 * 1024

SANDBOX_POD_TYPE = "sandbox-executor"
TRUSTED_POD_TYPE = "trusted-executor"


class K8sUnavailableError(Exception):
    """A Kubernetes source could not be read; `str(exc)` is shown to admins."""


def deployment_mode() -> str:
    """ "kubernetes" or "docker", derived from the executor settings.

    There is no separate deployment flag: a Kubernetes deploy is one whose
    executors are the k8s ones, or that the Helm chart stamped with
    K8S_RELEASE_NAME (always set by the chart, also with sandbox/trusted
    executors disabled or in-process).
    """
    if (
        settings.sandbox_executor.startswith("k8s")
        or settings.trusted_executor.startswith("k8s")
        or settings.k8s_release_name
    ):
        return "kubernetes"
    return "docker"


def release_namespace() -> str:
    """Namespace this process runs in — where the release's pods live."""
    ns = os.environ.get("POD_NAMESPACE")
    if ns:
        return ns
    try:
        with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace") as f:
            return f.read().strip()
    except OSError:
        from app.services.executor._k8s_runtime import resolve_namespace

        return resolve_namespace()


def executor_namespace() -> str:
    """Namespace executor pods are created in (k8s_sandbox_namespace or ours)."""
    from app.services.executor._k8s_runtime import resolve_namespace

    return resolve_namespace()


def _apis() -> tuple[Any, Any]:
    """(CoreV1Api, CustomObjectsApi). Shares the executor's loaded config."""
    from kubernetes import client

    from app.services.executor._k8s_runtime import _get_clients

    core, _ = _get_clients()
    return core, client.CustomObjectsApi()


def _selector(*extra: str) -> str:
    """Label selector scoped to this release.

    Without a release name there is no way to tell this deployment's pods
    from anything else in the namespace, so refuse rather than list them all.
    """
    if not settings.k8s_release_name:
        raise K8sUnavailableError(
            "K8S_RELEASE_NAME is not set, so this deployment's pods can't be told "
            "apart from others in the namespace (the Helm chart sets it)."
        )
    return ",".join([f"app.kubernetes.io/instance={settings.k8s_release_name}", *extra])


def _api_reason(e: Exception, what: str, namespace: str) -> str:
    """Admin-facing reason for a failed apiserver call (status only, no body)."""
    from kubernetes.client.exceptions import ApiException

    if isinstance(e, ApiException):
        if e.status in (401, 403):
            return (
                f"The backend's service account may not {what} in namespace "
                f"'{namespace}' (HTTP {e.status}). Enable systemStats.rbac in "
                f"the Helm chart, or grant get/list on that resource."
            )
        return f"Kubernetes API refused to {what} (HTTP {e.status} {e.reason or ''})".strip()
    return f"Kubernetes API not reachable ({type(e).__name__}: {e})"


# ── Parsing ──────────────────────────────────────────────────────────


def _quantity(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        from kubernetes.utils import parse_quantity

        return float(parse_quantity(value))
    except Exception:
        return None


def _component(pod: Any) -> str:
    labels = pod.metadata.labels or {}
    pod_type = labels.get("sinas.type")
    # Same service names the Docker path uses for executor containers.
    if pod_type == TRUSTED_POD_TYPE:
        return "shared"
    if pod_type == SANDBOX_POD_TYPE:
        return "sandbox"
    return labels.get("app.kubernetes.io/component") or pod.metadata.name


def _limits(pod: Any) -> tuple[float | None, float | None]:
    """Summed (cpu cores, memory bytes) limits; None if any container is unbounded."""
    cpu_total: float | None = 0.0
    mem_total: float | None = 0.0
    for c in (pod.spec.containers if pod.spec else None) or []:
        limits = (c.resources.limits if c.resources else None) or {}
        cpu = _quantity(limits.get("cpu"))
        mem = _quantity(limits.get("memory"))
        cpu_total = None if cpu is None or cpu_total is None else cpu_total + cpu
        mem_total = None if mem is None or mem_total is None else mem_total + mem
    return cpu_total or None, mem_total or None


def _status(pod: Any) -> tuple[str, str | None]:
    """Docker-like status word + the kubelet's reason when one explains it."""
    if pod.metadata.deletion_timestamp:
        return "terminating", None
    phase = (pod.status.phase if pod.status else None) or "Unknown"
    waiting = None
    for cs in (pod.status.container_statuses if pod.status else None) or []:
        if cs.state and cs.state.waiting and cs.state.waiting.reason:
            waiting = cs.state.waiting.reason
            break
    if phase == "Running":
        if waiting == "CrashLoopBackOff":
            return "restarting", waiting
        return "running", waiting
    mapping = {"Pending": "pending", "Succeeded": "completed", "Failed": "failed"}
    return mapping.get(phase, "unknown"), waiting or (pod.status.reason if pod.status else None)


def pod_info(pod: Any, now: datetime | None = None) -> dict[str, Any]:
    """One pod as a System-page service row (same keys as the Docker rows)."""
    now = now or datetime.now(timezone.utc)
    status, reason = _status(pod)
    statuses = (pod.status.container_statuses if pod.status else None) or []
    ready_count = sum(1 for cs in statuses if cs.ready)
    total = len(pod.spec.containers) if pod.spec and pod.spec.containers else len(statuses)
    restarts = sum(cs.restart_count or 0 for cs in statuses)

    started = (pod.status.start_time if pod.status else None) or pod.metadata.creation_timestamp
    uptime = int((now - started).total_seconds()) if started and status == "running" else 0
    created = pod.metadata.creation_timestamp
    age = int((now - created).total_seconds()) if created else None

    if status != "running":
        health = "none"
    elif total and ready_count == total:
        health = "healthy"
    else:
        # A fresh pod that hasn't passed readiness yet isn't "unhealthy".
        health = "starting" if (age or 0) < 120 and restarts == 0 else "unhealthy"

    return {
        "name": pod.metadata.name,
        "service": _component(pod),
        "status": status,
        "status_reason": reason,
        "health": health,
        "uptime_seconds": max(uptime, 0),
        "age_seconds": age,
        "created_at": created.isoformat() if created else "",
        "ready": f"{ready_count}/{total}",
        "restarts": restarts,
        "node": pod.spec.node_name if pod.spec else None,
    }


def _apply_metrics(info: dict[str, Any], pod: Any, usage: dict[str, float]) -> None:
    """Docker-compatible cpu_percent (100 = one core) + memory block."""
    cpu_limit, mem_limit = _limits(pod)
    cores = usage.get("cpu", 0.0)
    mem = usage.get("memory", 0.0)
    info["cpu_percent"] = round(cores * 100, 1)
    info["cpu_limit_cores"] = round(cpu_limit, 2) if cpu_limit else None
    info["memory"] = {
        "used_mb": round(mem / _MIB, 1),
        "limit_mb": round(mem_limit / _MIB, 1) if mem_limit else None,
        "percent": round(mem / mem_limit * 100, 1) if mem_limit else None,
    }


# ── Blocking API calls (run via asyncio.to_thread) ───────────────────


def _list_pods_blocking(namespace: str, selector: str) -> list[Any]:
    try:
        core, _ = _apis()
        return core.list_namespaced_pod(
            namespace=namespace, label_selector=selector, _request_timeout=_API_TIMEOUT
        ).items
    except Exception as e:
        raise K8sUnavailableError(_api_reason(e, "list pods", namespace)) from e


def _pod_metrics_blocking(namespace: str, selector: str) -> dict[str, dict[str, float]]:
    """pod name → {"cpu": cores, "memory": bytes}."""
    from kubernetes.client.exceptions import ApiException

    try:
        _, custom = _apis()
        resp = custom.list_namespaced_custom_object(
            group="metrics.k8s.io",
            version="v1beta1",
            namespace=namespace,
            plural="pods",
            label_selector=selector,
            _request_timeout=_API_TIMEOUT,
        )
    except ApiException as e:
        if e.status == 404:
            raise K8sUnavailableError(
                "CPU and memory need metrics-server, which is not installed in "
                "this cluster (the metrics.k8s.io API was not found)."
            ) from e
        if e.status == 503:
            raise K8sUnavailableError(
                "CPU and memory unavailable: the metrics.k8s.io API is registered "
                "but metrics-server is not serving yet."
            ) from e
        raise K8sUnavailableError(
            _api_reason(e, "read pod metrics (pods.metrics.k8s.io)", namespace)
        ) from e
    except Exception as e:
        raise K8sUnavailableError(_api_reason(e, "read pod metrics", namespace)) from e

    out: dict[str, dict[str, float]] = {}
    for item in resp.get("items", []):
        name = (item.get("metadata") or {}).get("name")
        if not name:
            continue
        cpu = mem = 0.0
        for c in item.get("containers") or []:
            usage = c.get("usage") or {}
            cpu += _quantity(usage.get("cpu")) or 0.0
            mem += _quantity(usage.get("memory")) or 0.0
        out[name] = {"cpu": cpu, "memory": mem}
    return out


# ── Async entry points ───────────────────────────────────────────────


async def list_services(
    include_stats: bool,
) -> tuple[list[dict[str, Any]], str | None, str | None]:
    """Release pods as service rows.

    Returns (services, services_unavailable_reason, metrics_unavailable_reason).
    Ephemeral sandbox pods are left out — they live for one execution and are
    shown on the sandbox card instead; warm trusted pods are included, like
    the Docker shared containers.
    """
    namespace = release_namespace()
    try:
        selector = _selector()
        pods = await asyncio.to_thread(_list_pods_blocking, namespace, selector)
    except K8sUnavailableError as e:
        logger.warning("System health: %s", e)
        return [], str(e), None

    pods = [p for p in pods if (p.metadata.labels or {}).get("sinas.type") != SANDBOX_POD_TYPE]
    now = datetime.now(timezone.utc)
    services = [pod_info(p, now) for p in pods]

    metrics_reason = None
    if include_stats:
        try:
            usage = await asyncio.to_thread(_pod_metrics_blocking, namespace, selector)
        except K8sUnavailableError as e:
            metrics_reason = str(e)
        else:
            for info, pod in zip(services, pods, strict=True):
                if info["status"] == "running" and info["name"] in usage:
                    _apply_metrics(info, pod, usage[info["name"]])

    priority = {"redis": 0, "postgres": 1, "pgbouncer": 2, "clickhouse": 3, "backend": 4}
    services.sort(key=lambda s: (priority.get(s["service"], 99), s["name"]))
    return services, None, metrics_reason


async def list_executor_pods(pod_type: str) -> list[dict[str, Any]]:
    """Executor pods of one kind (sandbox / trusted). Raises K8sUnavailableError."""
    selector = _selector(f"sinas.type={pod_type}")
    namespace = executor_namespace()
    pods = await asyncio.to_thread(_list_pods_blocking, namespace, selector)
    now = datetime.now(timezone.utc)
    return sorted((pod_info(p, now) for p in pods), key=lambda i: i["name"])
