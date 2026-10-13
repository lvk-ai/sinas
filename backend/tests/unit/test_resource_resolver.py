"""Which tools an agent's references resolve to.

Exact references now resolve in one query per kind instead of one each.
What they match must not change: an exact Function ref only matches an
active function, an exact Skill ref matches either way (as `get_by_name`
always did), and order, wildcards and duplicates behave as before.
"""
import uuid

from app.models.function import Function
from app.models.skill import Skill
from app.services.resource_resolver import resolve_resource_refs


def _ns() -> str:
    return f"rr{uuid.uuid4().hex[:8]}"


def _function(user, ns, name, active=True) -> Function:
    return Function(
        user_id=user.id, namespace=ns, name=name, code="def handler(input, context):\n    return {}\n",
        input_schema={"type": "object"}, output_schema={"type": "object"}, is_active=active,
    )


def _refs(resolved) -> list[str]:
    return [ref for ref, _ in resolved]


async def test_exact_function_refs_skip_inactive_and_missing(db, admin_user):
    ns = _ns()
    db.add_all([_function(admin_user, ns, "a"), _function(admin_user, ns, "off", active=False)])
    await db.flush()
    resolved = await resolve_resource_refs(db, [f"{ns}/off", f"{ns}/a", f"{ns}/missing"], Function)
    assert _refs(resolved) == [f"{ns}/a"]


async def test_exact_skill_refs_match_inactive_ones_as_before(db, admin_user):
    ns = _ns()
    db.add(Skill(user_id=admin_user.id, namespace=ns, name="off", description="d", content="c", is_active=False))
    await db.flush()
    resolved = await resolve_resource_refs(db, [{"skill": f"{ns}/off"}], Skill, ref_field="skill")
    assert _refs(resolved) == [f"{ns}/off"]


async def test_order_wildcards_and_duplicates(db, admin_user):
    ns, other = _ns(), _ns()
    db.add_all([
        _function(admin_user, ns, "b"), _function(admin_user, ns, "a"),
        _function(admin_user, ns, "off", active=False), _function(admin_user, other, "x"),
    ])
    await db.flush()
    resolved = await resolve_resource_refs(
        db, [f"{other}/x", f"{ns}/b", f"{ns}/*", f"{other}/x"], Function
    )
    # Refs in the order given; the wildcard adds what's new, active only;
    # each function once.
    assert _refs(resolved)[:2] == [f"{other}/x", f"{ns}/b"]
    assert sorted(_refs(resolved)[2:]) == [f"{ns}/a"]
    assert len({r.id for _, r in resolved}) == len(resolved)
