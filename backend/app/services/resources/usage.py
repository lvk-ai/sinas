"""What a resource points at, by kind and "namespace/name" — so a prune
never removes something a remaining resource still uses."""

from __future__ import annotations

from typing import Any

# Kinds whose resources point at others (as far as a removal can break them).
REFERRING_KINDS = ("schedules", "webhooks", "databaseTriggers", "collections", "agents", "components")

_TARGET_KIND = {"function": "functions", "agent": "agents", "pipeline": "pipelines"}


def _ns_name(namespace: Any, name: Any) -> str | None:
    return f"{namespace}/{name}" if namespace and name else None


def references_of(kind: str, spec: Any) -> set[tuple[str, str]]:
    """(kind, key) pairs `spec` (of resource kind `kind`) points at."""
    refs: set[tuple[str, str]] = set()
    if kind in ("schedules", "webhooks", "databaseTriggers"):
        target_type = getattr(spec, "schedule_type", None) or getattr(spec, "target_type", None)
        key = _ns_name(spec.target_namespace, spec.target_name)
        if target_type in _TARGET_KIND and key:
            refs.add((_TARGET_KIND[target_type], key))
    elif kind == "collections":
        for ref in (spec.content_filter_function, spec.post_upload_function):
            if ref:
                refs.add(("functions", ref))
    elif kind == "agents":
        from app.services.resources.agents import _reference_kinds, _refs

        for ref_kind, _label, _model in _reference_kinds():
            refs |= {(ref_kind, r) for r in _refs(spec, ref_kind)}
    elif kind == "components":
        refs |= {("agents", r) for r in spec.enabled_agents}
        refs |= {("functions", r) for r in spec.enabled_functions}
        refs |= {("queries", r) for r in spec.enabled_queries}
        refs |= {("components", r) for r in spec.enabled_components}
        refs |= {("stores", s.store) for s in spec.enabled_stores}
    return {(k, r) for k, r in refs if r and "*" not in r}
