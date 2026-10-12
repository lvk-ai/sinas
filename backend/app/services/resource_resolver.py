"""Resolve resource references with wildcard support.

Supports:
- "namespace/name"  — exact match
- "namespace/*"     — all active resources in namespace
- "*/*"             — all active resources

Used by tool discovery to resolve enabled_functions, enabled_queries,
enabled_collections, enabled_skills, etc.
"""
import logging
from typing import Any, Optional

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Exact refs follow each model's `get_by_name`: these only match active rows;
# the others (Skill, Query, Pipeline, ...) match a row either way. Models
# without `get_by_name` match active rows when they have `is_active`.
_EXACT_REFS_ACTIVE_ONLY = frozenset({"Function", "Agent", "Component", "LLMProvider"})


def matches_ref_pattern(ref: str, patterns: list) -> bool:
    """Check if a resource ref matches any pattern in a list.

    Handles exact matches, 'namespace/*', and '*/*' wildcards.
    Patterns can be strings or dicts with a ref field.
    """
    for pattern in patterns:
        if isinstance(pattern, str):
            p = pattern
        elif isinstance(pattern, dict):
            p = pattern.get("collection") or pattern.get("store") or pattern.get("skill") or ""
        elif hasattr(pattern, "collection"):
            p = pattern.collection
        elif hasattr(pattern, "store"):
            p = pattern.store
        else:
            continue

        if p == ref:
            return True
        if p == "*/*":
            return True
        if p.endswith("/*") and ref.startswith(p[:-1]):
            return True

    return False


async def resolve_resource_refs(
    db: AsyncSession,
    refs: list,
    model_class: Any,
    ref_field: str = "ref",
) -> list[tuple[str, Any]]:
    """Resolve a list of resource references to (ref_string, model_instance) pairs.

    Args:
        db: Database session
        refs: List of references — strings ("ns/name", "ns/*", "*/*") or
              dicts (for collections/stores with access mode — uses ref_field key)
        model_class: SQLAlchemy model with .namespace, .name, .is_active (optional)
        ref_field: For dict entries, the key containing the reference string
                   (e.g. "collection" for EnabledCollectionConfig)

    Returns:
        List of (original_ref_string, model_instance) tuples.
        Preserves order. Wildcards are expanded.
    """
    results: list[tuple[str, Any]] = []
    seen_ids = set()

    has_is_active = hasattr(model_class, "is_active")

    def ref_of(entry) -> str:
        """The reference string of a plain string or a dict/config entry."""
        if isinstance(entry, str):
            return entry
        if isinstance(entry, dict):
            return entry.get(ref_field, "")
        for field in (ref_field, "skill", "store", "collection"):
            if hasattr(entry, field):
                return getattr(entry, field)
        return ""

    refs = [ref_of(entry) for entry in refs]

    # Every exact ref in one query, not one query each.
    pairs = {
        tuple(ref.split("/", 1)) for ref in refs if ref and "/" in ref and not ref.endswith("/*")
    }
    exact: dict[tuple[str, str], Any] = {}
    if pairs:
        if hasattr(model_class, "get_by_name"):
            active_only = model_class.__name__ in _EXACT_REFS_ACTIVE_ONLY
        else:
            active_only = has_is_active
        query = select(model_class).where(
            tuple_(model_class.namespace, model_class.name).in_(list(pairs))
        )
        if active_only:
            query = query.where(model_class.is_active == True)
        for r in (await db.execute(query)).scalars().all():
            exact[(r.namespace, r.name)] = r

    for ref in refs:
        if not ref or "/" not in ref:
            continue

        if ref == "*/*":
            # All active resources
            query = select(model_class)
            if has_is_active:
                query = query.where(model_class.is_active == True)
            result = await db.execute(query)
            for r in result.scalars().all():
                if r.id not in seen_ids:
                    seen_ids.add(r.id)
                    results.append((f"{r.namespace}/{r.name}", r))

        elif ref.endswith("/*"):
            # All active resources in namespace
            ns = ref[:-2]
            query = select(model_class).where(model_class.namespace == ns)
            if has_is_active:
                query = query.where(model_class.is_active == True)
            result = await db.execute(query)
            for r in result.scalars().all():
                if r.id not in seen_ids:
                    seen_ids.add(r.id)
                    results.append((f"{r.namespace}/{r.name}", r))

        else:
            # Exact match
            ns, name = ref.split("/", 1)
            r = exact.get((ns, name))

            if r and r.id not in seen_ids:
                seen_ids.add(r.id)
                results.append((ref, r))
            elif not r:
                logger.warning(f"Resource {ref} not found")

    return results
