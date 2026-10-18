"""Package dependencies: a package that needs another package
(docs/design/package-dependencies.md).

A package declares `package.requires` — other packages by name, with an
optional version range, repository hint and install name. This module holds
the pure parts (version ranges) and the resolver that turns a package's
requirements into a plan: what is already installed and fits, what an active
package repository can supply, and what can't be met. PackageService acts on
the plan (preview, install, uninstall, upgrade checks).

Not the `dependencies` resource kind, which approves Python packages for
function containers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.package import Package

# ---------------------------------------------------------------------------
# Version ranges
# ---------------------------------------------------------------------------

_CLAUSE = re.compile(r"^\s*(~=|==|!=|>=|<=|>|<)\s*(v?[0-9][0-9A-Za-z.+-]*?)(\.\*)?\s*$")


@dataclass(frozen=True)
class Clause:
    op: str
    version: str
    wildcard: bool = False  # "==1.2.*" / "!=1.2.*"


def _key(version: str):
    from app.services.package_repositories import _version_key

    return _version_key(version)


def _release(version: str) -> tuple[int, ...]:
    """The release numbers as written (not padded): "1.2" -> (1, 2)."""
    version = version.strip().lstrip("v").split("+", 1)[0]
    parts = []
    for part in version.split("."):
        digits = re.match(r"^\d+", part)
        if not digits:
            break
        parts.append(int(digits.group(0)))
        if digits.group(0) != part:
            break  # "0-rc" ends the release part
    return tuple(parts)


def parse_range(text: str) -> list[Clause]:
    """A pyproject-style range: comma-separated clauses, each an operator
    (>=, >, <=, <, ==, !=, ~=) and a version; == and != also take a trailing
    .* ("==1.2.*"). Raises ValueError saying what's wrong."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("a version range can't be empty")
    clauses = []
    for raw in text.split(","):
        match = _CLAUSE.match(raw)
        if not match:
            raise ValueError(
                f"'{raw.strip()}' in version range '{text}' is not a clause like '>=1.2' "
                "(operators: >=, >, <=, <, ==, !=, ~=)"
            )
        op, version, star = match.group(1), match.group(2), bool(match.group(3))
        if star and op not in ("==", "!="):
            raise ValueError(f"'{raw.strip()}': only == and != take a .* wildcard")
        if _key(version) is None:
            raise ValueError(f"'{version}' in version range '{text}' is not a version")
        if op == "~=" and len(_release(version)) < 2:
            raise ValueError(f"'{raw.strip()}': ~= needs at least two version parts (~=1.2)")
        clauses.append(Clause(op, version, star))
    return clauses


def _prefix_match(version: str, prefix: str) -> bool:
    want = _release(prefix)
    have = _release(version) + (0,) * len(want)
    return have[: len(want)] == want


def _clause_holds(version: str, clause: Clause) -> bool:
    have, want = _key(version), _key(clause.version)
    if clause.op == "==":
        return _prefix_match(version, clause.version) if clause.wildcard else have == want
    if clause.op == "!=":
        return not (_prefix_match(version, clause.version) if clause.wildcard else have == want)
    if clause.op == ">=":
        return have >= want
    if clause.op == ">":
        return have > want
    if clause.op == "<=":
        return have <= want
    if clause.op == "<":
        return have < want
    # ~=1.4.2 is >=1.4.2 and ==1.4.*
    release = _release(clause.version)
    return have >= want and _prefix_match(version, ".".join(map(str, release[:-1])))


def satisfies(version: str | None, range_text: str | None) -> bool:
    """Whether `version` is in `range_text`. No range: any version. A version
    that isn't semver-style never satisfies a range. A pre-release satisfies
    a range only when one of its clauses names a pre-release (as in PEP 440,
    so "<2" doesn't admit 2.0.0-rc.1)."""
    if not range_text:
        return True
    if version is None or _key(version) is None:
        return False
    clauses = parse_range(range_text)
    is_pre = _key(version)[1] != (1,)
    if is_pre and not any(_key(c.version)[1] != (1,) for c in clauses):
        return False
    return all(_clause_holds(version, c) for c in clauses)


# ---------------------------------------------------------------------------
# Requirements as stored on a package record
# ---------------------------------------------------------------------------


def stored_requires(requirements: list[Any]) -> list[dict[str, Any]]:
    """PackageRequirement models as Package.requires keeps them."""
    return [
        {
            "name": r.name,
            "version": r.version,
            "repository": r.repository,
            "instance": r.instance,
            "install_name": r.install_name,
        }
        for r in requirements
    ]


def _target(entry: dict[str, Any]) -> str:
    return entry.get("install_name") or entry.get("instance") or entry.get("name")


def requirement_status(entry: dict[str, Any], installed: dict[str, Package]) -> dict[str, Any]:
    """A requirement (as stored, or as an index lists it) against what is
    installed now: status satisfied, missing or incompatible, with the
    installed version and package, and for incompatible the reason:
    "version" (out of range) or "other_package" (the install name holds a
    different package)."""
    target = _target(entry)
    package = installed.get(target)
    out = {
        **entry, "install_name": target, "installed_version": None,
        "installed_package": None, "reason": None,
    }
    if package is None:
        return {**out, "status": "missing"}
    out["installed_version"] = package.version
    out["installed_package"] = package.package_name or package.name
    if out["installed_package"] != entry.get("name"):
        return {**out, "status": "incompatible", "reason": "other_package"}
    try:
        in_range = satisfies(package.version, entry.get("version"))
    except ValueError:
        in_range = False  # an unparseable range (an index is read leniently)
    if not in_range:
        return {**out, "status": "incompatible", "reason": "version"}
    return {**out, "status": "satisfied"}


def dependents_of(install_name: str, packages: list[Package]) -> list[Package]:
    """Installed packages with a requirement targeting `install_name`."""
    return [
        p for p in packages
        if p.name != install_name and any(_target(e) == install_name for e in (p.requires or []))
    ]


def dependency_status(shown: list[Package], installed: list[Package]) -> dict[Any, dict[str, Any]]:
    """Per shown package: its requirements with their status now, the
    installs requiring it, and whether it is an orphan (installed as a
    dependency, required by nothing any more)."""
    by_name = {p.name: p for p in installed}
    out = {}
    for package in shown:
        required_by = sorted(d.name for d in dependents_of(package.name, installed))
        out[package.id] = {
            "requires": [requirement_status(e, by_name) for e in (package.requires or [])],
            "required_by": required_by,
            "installed_as_dependency": bool(package.installed_as_dependency),
            "orphaned": bool(package.installed_as_dependency) and not required_by,
        }
    return out


def broken_dependents(
    install_name: str, package_name: str, version: str, packages: list[Package]
) -> list[dict[str, Any]]:
    """The installed packages whose requirement on `install_name` the given
    package and version would no longer meet (an upgrade, or another package
    put under that name)."""
    breaks = []
    for dependent in dependents_of(install_name, packages):
        for entry in dependent.requires or []:
            if _target(entry) != install_name:
                continue
            if entry.get("name") != package_name or not satisfies(version, entry.get("version")):
                breaks.append({
                    "dependent": dependent.name,
                    "requires": entry.get("name"),
                    "version": entry.get("version"),
                })
    return breaks


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

SATISFIED = "satisfied"
INSTALL = "install"
MISSING = "missing"
INCOMPATIBLE = "incompatible"
CYCLE = "cycle"
INVALID = "invalid"
BLOCKING = (MISSING, INCOMPATIBLE, CYCLE, INVALID)

MAX_DEPTH = 10


@dataclass
class PlanEntry:
    """One required install: what it is, where it comes from, and whether it
    can be had."""

    install_name: str
    package_name: str
    status: str
    constraints: list[dict[str, Any]] = field(default_factory=list)  # [{required_by, version}]
    version: str | None = None            # installed, or the one that will be
    installed_version: str | None = None
    repository: str | None = None         # where an INSTALL entry comes from
    message: str | None = None
    # For INSTALL entries: the fetched (checksum-verified) YAML and its URL.
    yaml: str | None = field(default=None, repr=False)
    file_url: str | None = None
    requires: list[str] = field(default_factory=list)  # install names it requires

    @property
    def required_by(self) -> list[str]:
        return sorted({c["required_by"] for c in self.constraints})

    @property
    def instance(self) -> str | None:
        """The `instance` to install it under (None: its own name)."""
        return None if self.install_name == self.package_name else self.install_name

    def as_dict(self) -> dict[str, Any]:
        return {
            "install_name": self.install_name,
            "package_name": self.package_name,
            "status": self.status,
            "version": self.version,
            "installed_version": self.installed_version,
            "repository": self.repository,
            "constraints": self.constraints,
            "required_by": self.required_by,
            "requires": self.requires,
            "message": self.message,
        }


@dataclass
class Plan:
    """Dependencies first, in install order (a dependency before whatever
    requires it). The root package itself is not in it."""

    entries: list[PlanEntry] = field(default_factory=list)

    @property
    def to_install(self) -> list[PlanEntry]:
        return [e for e in self.entries if e.status == INSTALL]

    @property
    def blocking(self) -> list[PlanEntry]:
        return [e for e in self.entries if e.status in BLOCKING]

    def errors(self) -> list[str]:
        return [f"Requirement '{e.install_name}': {e.message}" for e in self.blocking]


def _requirements_of(yaml_content: str, values: dict[str, Any] | None = None) -> list[Any]:
    """package.requires of a package YAML, validated. Variable values given
    (and declared defaults) are substituted first, so a requirement may name
    its instance through a variable. YAML that doesn't parse, or has no
    package metadata, requires nothing here: the package's own validation
    reports it."""
    from app.schemas.config import PackageMetadataConfig

    text = _substitute_known(yaml_content, values or {})
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    meta = doc.get("package") if isinstance(doc, dict) else None
    if not isinstance(meta, dict) or not meta.get("requires"):
        return []
    return PackageMetadataConfig.model_validate(meta).requires


_VAR = re.compile(r"\$\{\{\s*vars\.([A-Z][A-Z0-9_]*)\s*\}\}")


def _substitute_known(yaml_content: str, values: dict[str, Any]) -> str:
    try:
        doc = yaml.safe_load(yaml_content)
    except Exception:
        return yaml_content
    declared = {}
    spec = doc.get("spec") if isinstance(doc, dict) else None
    declarations = spec.get("variables") if isinstance(spec, dict) else None
    for decl in declarations if isinstance(declarations, list) else []:
        if isinstance(decl, dict) and isinstance(decl.get("name"), str):
            declared[decl["name"]] = decl.get("default")

    def sub(m):
        name = m.group(1)
        value = values.get(name, declared.get(name))
        return m.group(0) if value is None else str(value)

    return _VAR.sub(sub, yaml_content)


async def installed_packages(db: AsyncSession) -> dict[str, Package]:
    return {p.name: p for p in (await db.execute(select(Package))).scalars()}


def _candidates(repositories: list, name: str, hint: str | None, dependent_repo: str | None):
    """Active repositories offering `name`, in preference order: the hint,
    the dependent's own repository, then the rest by name."""
    offering = []
    for repository in repositories:
        entry = next((p for p in (repository.index or {}).get("packages", []) if p["name"] == name), None)
        if entry is not None:
            offering.append((repository, entry))

    def rank(item):
        repository = item[0]
        if hint and repository.name == hint:
            return (0, repository.name)
        if dependent_repo and repository.name == dependent_repo:
            return (1, repository.name)
        return (2, repository.name)

    return sorted(offering, key=rank)


async def resolve_plan(
    db: AsyncSession,
    *,
    root_name: str,
    root_install_name: str,
    requirements: list[Any],
    root_repository: str | None = None,
    dependency_variables: dict[str, dict[str, Any]] | None = None,
) -> Plan:
    """Resolve the root package's requirements against what is installed and
    what the active package repositories offer, recursively.

    A satisfied requirement is not descended into (its own requirements were
    checked when it was installed). A package to install is fetched and
    checked against its index checksum, and its own requirements resolved.
    Cycles, packages no active repository offers in range, and installed
    packages out of range are entries with a blocking status, never raised:
    the preview shows them all at once.
    """
    from app.services import package_repositories as repos
    from app.services.package_service import resolve_install_name

    installed = await installed_packages(db)
    repositories = await repos._repositories(db)
    dependency_variables = dependency_variables or {}
    entries: dict[str, PlanEntry] = {}
    order: list[PlanEntry] = []

    async def visit(req, required_by: str, path: list[str], dependent_repo: str | None, depth: int):
        target = req.install_name
        constraint = {"required_by": required_by, "version": req.version}

        if target in path:
            cycle = " → ".join(path[path.index(target):] + [target])
            entry = entries.get(target)
            if entry is None:
                entry = PlanEntry(install_name=target, package_name=req.name, status=CYCLE)
                entries[target] = entry
                order.append(entry)
            entry.status, entry.message = CYCLE, f"circular requirement: {cycle}"
            entry.constraints.append(constraint)
            return

        entry = entries.get(target)
        if entry is not None:
            # Required again (a diamond): every constraint must hold.
            entry.constraints.append(constraint)
            if entry.status in BLOCKING:
                return
            if entry.package_name != req.name:
                entry.status = INCOMPATIBLE
                entry.message = (
                    f"'{required_by}' requires package '{req.name}' as '{target}', "
                    f"but '{target}' is package '{entry.package_name}'"
                )
            elif not satisfies(entry.version, req.version):
                which = "installed" if entry.status == SATISFIED else "planned"
                entry.status = INCOMPATIBLE
                entry.message = (
                    f"'{required_by}' requires {req.name} {req.version}, but the {which} "
                    f"version is {entry.version}"
                )
            return

        package = installed.get(target)
        if package is not None:
            declared = package.package_name or package.name
            entry = PlanEntry(
                install_name=target, package_name=declared, status=SATISFIED,
                constraints=[constraint], version=package.version, installed_version=package.version,
            )
            if declared != req.name:
                entry.status = INCOMPATIBLE
                entry.message = f"'{target}' is installed, but it is package '{declared}', not '{req.name}'"
            elif not satisfies(package.version, req.version):
                entry.status = INCOMPATIBLE
                entry.message = (
                    f"{req.name} {package.version} is installed, but '{required_by}' requires "
                    f"{req.version}; update it first"
                )
            entries[target] = entry
            order.append(entry)
            return

        entry = PlanEntry(install_name=target, package_name=req.name, status=MISSING, constraints=[constraint])
        entries[target] = entry
        if depth > MAX_DEPTH:
            entry.message = f"requirements nest deeper than {MAX_DEPTH} levels"
            order.append(entry)
            return

        candidates = _candidates(repositories, req.name, req.repository, dependent_repo)
        chosen = next(
            ((r, e) for r, e in candidates if satisfies(e["version"], req.version)), None
        )
        if chosen is None:
            if candidates:
                offered = ", ".join(f"{e['version']} in {r.name}" for r, e in candidates)
                entry.message = (
                    f"not installed, and no active repository offers {req.name} {req.version} "
                    f"(offered: {offered})"
                )
            else:
                entry.message = f"not installed, and no active package repository offers '{req.name}'"
            order.append(entry)
            return

        repository, index_entry = chosen
        try:
            text, fetched, url = await repos.fetch_package(db, repository, req.name)
        except repos.RepositoryError as e:
            entry.message = f"could not be fetched from '{repository.name}': {e}"
            order.append(entry)
            return
        if not satisfies(fetched["version"], req.version):
            # The index moved on while fetching (a refresh).
            entry.message = f"{repository.name} now offers {req.name} {fetched['version']}, outside {req.version}"
            order.append(entry)
            return

        entry.version, entry.repository, entry.file_url = fetched["version"], repository.name, url
        try:
            # It must be installable under that name (multiInstance rule).
            _name, substituted, _declared, _multi = resolve_install_name(text, entry.instance)
            nested = _requirements_of(substituted, dependency_variables.get(target))
        except Exception as e:  # ValueError, yaml / pydantic errors
            entry.status = INVALID
            entry.message = f"{req.name} {fetched['version']} from {repository.name} can't be installed as '{target}': {e}"
            order.append(entry)
            return
        entry.status, entry.yaml = INSTALL, text
        entry.requires = [r.install_name for r in nested]
        for sub in nested:
            await visit(sub, target, path + [target], repository.name, depth + 1)
        order.append(entry)  # after what it requires

    for req in requirements:
        await visit(req, root_install_name, [root_install_name], root_repository, 1)
    return Plan(entries=order)


async def lock_packages(db: AsyncSession) -> None:
    """Serialize package installs and uninstalls until the transaction ends,
    so an uninstall can't remove a package between a dependent's plan seeing
    it installed and the dependent's commit. Taken before any row lock an
    install takes (lock order: before lock_singleton)."""
    import zlib

    from sqlalchemy import text

    await db.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": zlib.crc32(b"sinas:packages")}
    )
