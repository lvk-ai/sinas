"""Resolve resource references with wildcard support.

Supports:
- "namespace/name"  — exact match
- "namespace/*"     — all active resources in namespace
- "*/*"             — all active resources

Used by tool discovery to resolve every agent `enabled_*` list (functions,
queries, pipelines, skills, components, connectors, MCP servers, stores,
collections, agents), and by execution to check a call against them.

An exact reference is the agent author's grant of that one resource and
behaves as it always did. A wildcard is a blanket: it sweeps in resources
the author never named, including ones created later, by anyone. So a
wildcard only expands to resources the chat's user may use themselves:
`can_access` (the permission the runtime checks for that kind) is applied to
wildcard matches, never to exact ones.
"""
import logging
from typing import Any, Callable, Optional

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Exact refs follow each model's `get_by_name`: these only match active rows;
# the others (Skill, Query, Pipeline, ...) match a row either way. Models
# without `get_by_name` match active rows when they have `is_active`.
_EXACT_REFS_ACTIVE_ONLY = frozenset({"Function", "Agent", "Component", "LLMProvider"})

# The key holding the reference in a dict entry, per kind of entry.
_REF_FIELDS = ("collection", "store", "skill", "connector", "server")

# How specific a matching entry is: an exact entry beats "ns/*", which beats "*/*".
EXACT, NAMESPACE, ALL = 2, 1, 0

SUPPORTED_PATTERNS = "'namespace/name', 'namespace/*' or '*/*'"


def ref_specificity(ref: Any) -> Optional[int]:
    """EXACT, NAMESPACE or ALL for a well-formed reference, else None: no
    namespace, or a "*" anywhere but as the whole name ("ns/*") or the whole
    reference ("*/*")."""
    if not isinstance(ref, str) or "/" not in ref:
        return None
    namespace, name = ref.split("/", 1)
    if not namespace or not name:
        return None
    if ref == "*/*":
        return ALL
    if name == "*" and "*" not in namespace:
        return NAMESPACE
    if "*" in ref:
        return None  # "*/name", "ns/a*", "n*/x": nothing resolves these
    return EXACT


def is_wildcard_ref(ref: Any) -> bool:
    """Whether ref is a supported wildcard ("namespace/*" or "*/*")."""
    return ref_specificity(ref) in (NAMESPACE, ALL)


def wildcard_ref_problem(ref: Any) -> Optional[str]:
    """Why a reference containing "*" can't be resolved, or None. Only a
    whole name is a wildcard: anything else would silently match nothing."""
    if not isinstance(ref, str) or "*" not in ref or is_wildcard_ref(ref):
        return None
    return f"'{ref}' is not a supported pattern (use {SUPPORTED_PATTERNS})"


def entry_ref(entry: Any, ref_field: Optional[str] = None) -> str:
    """The reference string of a plain string, a dict entry or a config object."""
    if isinstance(entry, str):
        return entry
    fields = (ref_field, *_REF_FIELDS) if ref_field else _REF_FIELDS
    if isinstance(entry, dict):
        for field in fields:
            if isinstance(entry.get(field), str) and entry[field]:
                return entry[field]
        return ""
    for field in fields:
        value = getattr(entry, field, None)
        if isinstance(value, str) and value:
            return value
    return ""


def entry_access(entry: Any) -> str:
    """An entry's access mode: "readwrite" only when it says so."""
    if isinstance(entry, dict):
        return "readwrite" if entry.get("access") == "readwrite" else "readonly"
    return "readwrite" if getattr(entry, "access", None) == "readwrite" else "readonly"


def _match(ref: str, pattern: str) -> Optional[int]:
    """The specificity with which pattern matches ref, or None."""
    if pattern == ref:
        return EXACT
    specificity = ref_specificity(pattern)
    if specificity == ALL:
        return ALL
    if specificity == NAMESPACE and ref.startswith(pattern[:-1]):
        return NAMESPACE
    return None


def matching_entries(ref: str, patterns: list, ref_field: Optional[str] = None) -> tuple[Optional[int], list]:
    """The most specific entries matching ref, as (specificity, entries), or
    (None, []) when none does. Equally specific entries are all returned."""
    best: Optional[int] = None
    found: list = []
    for entry in patterns or []:
        specificity = _match(ref, entry_ref(entry, ref_field))
        if specificity is None:
            continue
        if best is None or specificity > best:
            best, found = specificity, [entry]
        elif specificity == best:
            found.append(entry)
    return best, found


def ref_access(ref: str, entries: list, ref_field: Optional[str] = None) -> Optional[str]:
    """The access mode ("readonly"/"readwrite") entries with an access mode
    (stores, collections) give ref, or None when none matches. The most
    specific entry decides — an exact entry over "ns/*" over "*/*" — and
    equally specific ones combine: any readwrite among them makes it
    readwrite."""
    _, found = matching_entries(ref, entries, ref_field)
    if not found:
        return None
    return "readwrite" if any(entry_access(e) == "readwrite" for e in found) else "readonly"


def matches_ref_pattern(ref: str, patterns: list) -> bool:
    """Check if a resource ref matches any pattern in a list.

    Handles exact matches, 'namespace/*', and '*/*' wildcards.
    Patterns can be strings or dicts/objects with a ref field.
    """
    return matching_entries(ref, patterns)[0] is not None


def ref_granted(
    ref: str, patterns: list, resource: Any, can_access: Callable[[Any], bool],
    ref_field: Optional[str] = None,
) -> bool:
    """Whether the entries grant this resource at call time, as discovery
    resolves them: an exact entry does; a wildcard only when the user may
    use the resource (`can_access`)."""
    specificity, _ = matching_entries(ref, patterns, ref_field)
    if specificity is None:
        return False
    return specificity == EXACT or can_access(resource)


async def resolve_resource_refs(
    db: AsyncSession,
    refs: list,
    model_class: Any,
    ref_field: str = "ref",
    *,
    can_access: Optional[Callable[[Any], bool]] = None,
    scope: Any = None,
) -> list[tuple[str, Any]]:
    """Resolve a list of resource references to (ref_string, model_instance) pairs.

    Args:
        db: Database session
        refs: List of references — strings ("ns/name", "ns/*", "*/*") or
              dicts (for collections/stores with access mode — uses ref_field key)
        model_class: SQLAlchemy model with .namespace, .name, .is_active (optional)
        ref_field: For dict entries, the key containing the reference string
                   (e.g. "collection" for EnabledCollectionConfig)
        can_access: Whether the chat's user may use a resource; a wildcard
                    match is left out when it says no (exact refs never are)
        scope: Extra condition every lookup applies (e.g. a Collection's kind)

    Returns:
        List of (original_ref_string, model_instance) tuples.
        Preserves order. Wildcards are expanded.
    """
    results: list[tuple[str, Any]] = []
    seen_ids = set()

    has_is_active = hasattr(model_class, "is_active")

    refs = [entry_ref(entry, ref_field) for entry in refs]

    def scoped(query):
        return query.where(scope) if scope is not None else query

    # Every exact ref in one query, not one query each.
    pairs = {tuple(ref.split("/", 1)) for ref in refs if ref_specificity(ref) == EXACT}
    exact: dict[tuple[str, str], Any] = {}
    if pairs:
        if hasattr(model_class, "get_by_name"):
            active_only = model_class.__name__ in _EXACT_REFS_ACTIVE_ONLY
        else:
            active_only = has_is_active
        query = scoped(select(model_class).where(
            tuple_(model_class.namespace, model_class.name).in_(list(pairs))
        ))
        if active_only:
            query = query.where(model_class.is_active == True)
        for r in (await db.execute(query)).scalars().all():
            exact[(r.namespace, r.name)] = r

    for ref in refs:
        specificity = ref_specificity(ref)
        if specificity is None:
            if ref and "*" in ref:
                logger.warning(f"Unsupported reference pattern {ref!r} (use {SUPPORTED_PATTERNS})")
            continue

        if specificity in (ALL, NAMESPACE):
            # All active resources, or all active ones in the namespace
            query = scoped(select(model_class))
            if specificity == NAMESPACE:
                query = query.where(model_class.namespace == ref[:-2])
            if has_is_active:
                query = query.where(model_class.is_active == True)
            result = await db.execute(query)
            for r in result.scalars().all():
                if r.id in seen_ids:
                    continue
                if can_access is not None and not can_access(r):
                    continue  # not the user's to use: an exact ref may still add it
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


async def resolve_access_entries(
    db: AsyncSession,
    entries: list,
    model_class: Any,
    ref_field: str,
    *,
    can_read: Optional[Callable[[Any], bool]] = None,
    can_write: Optional[Callable[[Any], bool]] = None,
    scope: Any = None,
) -> list[tuple[str, str, Any]]:
    """Resolve entries that carry an access mode (stores, collections) to
    (ref, access, resource) triples, in the order the entries name them.

    A wildcard entry's mode applies to every resource it matches. When
    several entries match one resource, the most specific decides (an exact
    entry over "ns/*" over "*/*", see `ref_access`), so
    `[{"ns/*": readwrite}, {"ns/archive": readonly}]` keeps the archive read
    only. A resource reached only through a wildcard is included only when
    `can_read` allows it, and is read only unless `can_write` allows writes —
    tools the user's own permissions would refuse are never offered. A plain
    string entry is read only."""
    best: dict[Any, list] = {}  # id -> [specificity, access, ref, resource]
    order: list = []
    for entry in entries or []:
        ref = entry_ref(entry, ref_field)
        specificity = ref_specificity(ref)
        if specificity is None:
            if ref:
                logger.warning(f"Invalid reference {ref!r} (use {SUPPORTED_PATTERNS})")
            continue
        access = entry_access(entry)
        for resolved_ref, resource in await resolve_resource_refs(db, [ref], model_class, scope=scope):
            current = best.get(resource.id)
            if current is None:
                best[resource.id] = [specificity, access, resolved_ref, resource]
                order.append(resource.id)
            elif specificity > current[0]:
                current[0], current[1], current[2] = specificity, access, resolved_ref
            elif specificity == current[0] and access == "readwrite":
                current[1] = "readwrite"

    results: list[tuple[str, str, Any]] = []
    for resource_id in order:
        specificity, access, ref, resource = best[resource_id]
        if specificity != EXACT:
            if can_read is not None and not can_read(resource):
                continue
            if access == "readwrite" and can_write is not None and not can_write(resource):
                access = "readonly"
        results.append((ref, access, resource))
    return results


def user_can(user_id: Optional[str], permissions: Optional[dict[str, bool]], action: str) -> Callable[[Any], bool]:
    """A resource's own access check (PermissionMixin.can_user_access, as the
    REST API applies it) for this user and action; no user: nothing."""

    def check(resource: Any) -> bool:
        if not user_id:
            return False
        return resource.can_user_access(str(user_id), permissions or {}, action)

    return check


def user_has(
    user_id: Optional[str], permissions: Optional[dict[str, bool]], kind: str, action: str
) -> Callable[[Any], bool]:
    """The runtime's permission string check, e.g. sinas.stores/<ns>/<name>.read_state:own
    (as the kinds whose execution checks a plain permission apply it)."""
    from app.core.permissions import check_permission

    def check(resource: Any) -> bool:
        if not user_id:
            return False
        return check_permission(
            permissions or {}, f"sinas.{kind}/{resource.namespace}/{resource.name}.{action}:own"
        )

    return check
