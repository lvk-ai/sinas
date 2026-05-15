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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


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

    for entry in refs:
        # Extract the reference string from either a plain string or dict
        if isinstance(entry, str):
            ref = entry
        elif isinstance(entry, dict):
            ref = entry.get(ref_field, "")
        elif hasattr(entry, ref_field):
            ref = getattr(entry, ref_field)
        elif hasattr(entry, "skill"):
            ref = entry.skill  # EnabledSkillConfig
        elif hasattr(entry, "store"):
            ref = entry.store  # EnabledStoreConfig
        elif hasattr(entry, "collection"):
            ref = entry.collection  # EnabledCollectionConfig
        else:
            continue

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
            if hasattr(model_class, "get_by_name"):
                r = await model_class.get_by_name(db, ns, name)
            else:
                query = select(model_class).where(
                    model_class.namespace == ns, model_class.name == name
                )
                if has_is_active:
                    query = query.where(model_class.is_active == True)
                result = await db.execute(query)
                r = result.scalar_one_or_none()

            if r and r.id not in seen_ids:
                seen_ids.add(r.id)
                results.append((ref, r))
            elif not r:
                logger.warning(f"Resource {ref} not found")

    return results
