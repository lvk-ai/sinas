"""Package dependencies (docs/design/package-dependencies.md).

A package declares the packages it needs in package.requires. Preview
resolves them against what is installed and what the active package
repositories offer; install installs the missing ones first, in the same
transaction; uninstall refuses while dependents exist; an upgrade that
breaks a dependent's range is refused unless forced.

Repositories here are rows with an index and files served by an httpx
MockTransport: no network.
"""
import hashlib
import json
import uuid

import httpx
import pytest
import yaml
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.package import Package
from app.models.package_repository import PackageRepository
from app.models.skill import Skill
from app.schemas.config import PackageMetadataConfig
from app.services import package_repositories as repos
from app.services.package_dependencies import parse_range, satisfies
from app.services.package_service import PackageInUse, PackageService
from tests.conftest import auth_headers


def _uid() -> str:
    return uuid.uuid4().hex[:6]


def package_yaml(
    name: str,
    version: str = "1.0.0",
    *,
    requires: list[dict] | None = None,
    uses_skills: list[str] = (),
    variable: str | None = None,
    multi: bool = False,
    manifest_requires: list[dict] | None = None,
) -> str:
    """A package with one skill (<namespace>/hello) and, when it uses
    others' skills, an agent enabling them."""
    ns = "${{ install.name }}" if multi else name
    meta: dict = {"name": name, "version": version}
    if multi:
        meta["multiInstance"] = True
    if requires:
        meta["requires"] = requires
    spec: dict = {
        "skills": [{
            "namespace": ns, "name": "hello", "description": "Says hello",
            "content": f"${{{{ vars.{variable} }}}}" if variable else "Say hello.",
        }],
    }
    if variable:
        spec["variables"] = [{"name": variable, "type": "text", "description": "a value"}]
    if uses_skills:
        spec["agents"] = [{
            "namespace": ns, "name": "worker", "description": "uses another package",
            "systemPrompt": "Work.", "enabledSkills": list(uses_skills),
        }]
    if manifest_requires:
        spec["manifests"] = [{"namespace": ns, "name": "app", "requiredResources": manifest_requires}]
    return yaml.safe_dump(
        {"apiVersion": "sinas.co/v1", "kind": "SinasPackage", "package": meta, "spec": spec},
        sort_keys=False,
    )


class FakeRepositories:
    """Package repositories as rows with an index; their files served by a
    MockTransport, keyed by full URL."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        body = self.files.get(str(request.url))
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404)

    async def add(self, db: AsyncSession, user, name: str, packages: list[str],
                  extra: dict[str, dict] | None = None) -> PackageRepository:
        base = f"https://{name}.example.com/"
        entries = []
        for text in packages:
            meta = yaml.safe_load(text)["package"]
            path = f"packages/{meta['name']}/package.yaml"
            raw = text.encode()
            self.files[base + path] = raw
            entries.append({
                "name": meta["name"], "version": str(meta["version"]), "path": path,
                "sha256": hashlib.sha256(raw).hexdigest(),
                **((extra or {}).get(meta["name"], {})),
            })
        index = repos.parse_index(json.dumps({"schema": repos.INDEX_SCHEMA, "packages": entries}).encode())
        row = PackageRepository(name=name, url=base, auth={}, is_active=True, user_id=user.id, index=index)
        db.add(row)
        await db.flush()
        return row


@pytest.fixture
def fake_repos(monkeypatch):
    fake = FakeRepositories()
    real = httpx.AsyncClient
    monkeypatch.setattr(
        "app.services.package_repositories.httpx.AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(fake.handler), **kw),
    )
    return fake


async def _installed(db: AsyncSession, name: str):
    return (await db.execute(select(Package).where(Package.name == name))).scalar_one_or_none()


def _deps(info: dict) -> dict[str, dict]:
    return {d["install_name"]: d for d in info["dependencies"]}


# ---------------------------------------------------------------- ranges


class TestRanges:
    @pytest.mark.parametrize("version, spec, ok", [
        ("1.2.0", ">=1.2,<2", True),
        ("1.9.9", ">=1.2,<2", True),
        ("2.0.0", ">=1.2,<2", False),
        ("1.1.9", ">=1.2,<2", False),
        ("1.4.5", "~=1.4", True),        # >=1.4, ==1.*
        ("2.0.0", "~=1.4", False),
        ("1.4.9", "~=1.4.2", True),      # >=1.4.2, ==1.4.*
        ("1.5.0", "~=1.4.2", False),
        ("1.2.7", "==1.2.*", True),
        ("1.3.0", "==1.2.*", False),
        ("1.2.0", "==1.2", True),        # 1.2 == 1.2.0
        ("1.2.1", "!=1.2.1", False),
        ("1.2.2", "!=1.2.*", False),
        ("1.0.0", ">1.0.0", False),
        ("1.0.0", "<=1.0.0", True),
        ("v1.2.0", ">=1.2", True),
        ("2.0.0-rc.1", "<2", False),     # pre-release: only when a clause names one
        ("2.0.0-rc.1", ">=2.0.0-rc.1", True),
        ("1.5.0-beta", ">=1.0", False),
        ("weird", ">=1.0", False),       # not semver-style: never in a range
    ])
    def test_satisfies(self, version, spec, ok):
        assert satisfies(version, spec) is ok

    def test_no_range_is_any_version(self):
        assert satisfies("weird", None) and satisfies("0.0.1", "")

    @pytest.mark.parametrize("spec", ["", "1.2", ">=", "=>1.2", ">=1.2,", ">=abc", "~=1", ">=1.2.*", "^1.2"])
    def test_invalid_ranges(self, spec):
        with pytest.raises(ValueError):
            parse_range(spec)


class TestDeclaration:
    def test_requires_is_validated(self):
        meta = PackageMetadataConfig.model_validate({
            "name": "triage", "requires": [{"name": "gmail", "version": ">=1.2,<2", "repository": "sinas-official"}],
        })
        (req,) = meta.requires
        assert (req.name, req.version, req.install_name) == ("gmail", ">=1.2,<2", "gmail")

    def test_omitted_or_null_is_empty(self):
        assert PackageMetadataConfig.model_validate({"name": "x"}).requires == []
        assert PackageMetadataConfig.model_validate({"name": "x", "requires": None}).requires == []

    @pytest.mark.parametrize("requires", [
        [{"name": "gmail", "version": "1.2"}],              # not a range
        [{"name": "Gmail"}],                                # not a package name
        [{"name": "gmail", "instance": "Bad Name"}],
        [{"name": "gmail"}, {"name": "gmail"}],             # twice
        [{"name": "x"}],                                    # itself
        [{"name": "gmail", "versions": ">=1"}],             # unknown key
    ])
    def test_bad_requirements_are_refused(self, requires):
        with pytest.raises(ValidationError):
            PackageMetadataConfig.model_validate({"name": "x", "requires": requires})

    def test_two_instances_of_one_package(self):
        meta = PackageMetadataConfig.model_validate({
            "name": "x", "requires": [{"name": "mail", "instance": "mail-a"}, {"name": "mail", "instance": "mail-b"}],
        })
        assert [r.install_name for r in meta.requires] == ["mail-a", "mail-b"]


# ---------------------------------------------------------------- plans


class TestPlan:
    async def test_satisfied(self, db, admin_user, fake_repos):
        dep, root = f"gmail{_uid()}", f"triage{_uid()}"
        service = PackageService(db)
        await service.install(package_yaml(dep, "1.3.0"), str(admin_user.id))
        result, _, _, info = await service.preview(
            package_yaml(root, requires=[{"name": dep, "version": ">=1.2,<2"}], uses_skills=[f"{dep}/hello"]),
            str(admin_user.id),
        )
        assert result.success, result.errors
        assert _deps(info)[dep]["status"] == "satisfied" and _deps(info)[dep]["installed_version"] == "1.3.0"

    async def test_missing_but_available_is_installed_first(self, db, admin_user, fake_repos):
        dep, root = f"gmail{_uid()}", f"triage{_uid()}"
        repo = await fake_repos.add(db, admin_user, f"r{_uid()}", [package_yaml(dep, "1.3.0")])
        root_yaml = package_yaml(root, requires=[{"name": dep, "version": ">=1.2"}], uses_skills=[f"{dep}/hello"])
        service = PackageService(db)

        # The root's agent refers to the dependency's skill: the dry run
        # counts what the planned dependency declares.
        result, _, _, info = await service.preview(root_yaml, str(admin_user.id))
        assert result.success, result.errors
        entry = _deps(info)[dep]
        assert (entry["status"], entry["version"], entry["repository"]) == ("install", "1.3.0", repo.name)
        assert entry["required_by"] == [root] and entry["preview"]["success"]
        assert await _installed(db, dep) is None  # a dry run

        package, _ = await service.install(root_yaml, str(admin_user.id))
        assert [p.name for p, _ in service.installed_dependencies] == [dep]
        dep_row = await _installed(db, dep)
        assert dep_row.installed_as_dependency and dep_row.source_repository == repo.name
        assert not package.installed_as_dependency
        assert package.requires == [{
            "name": dep, "version": ">=1.2", "repository": None, "instance": None, "install_name": dep,
        }]
        assert await Skill.get_by_name(db, dep, "hello") is not None

    async def test_missing_and_unavailable_is_refused(self, db, admin_user, fake_repos):
        root = f"triage{_uid()}"
        root_yaml = package_yaml(root, requires=[{"name": f"nothere{_uid()}"}])
        service = PackageService(db)
        result, _, _, info = await service.preview(root_yaml, str(admin_user.id))
        assert not result.success
        (entry,) = info["dependencies"]
        assert entry["status"] == "missing" and "no active package repository" in entry["message"]
        with pytest.raises(ValueError, match="requirements can't be met"):
            await service.install(root_yaml, str(admin_user.id))
        assert await _installed(db, root) is None

    async def test_available_only_out_of_range_is_missing(self, db, admin_user, fake_repos):
        dep, root = f"gmail{_uid()}", f"triage{_uid()}"
        await fake_repos.add(db, admin_user, f"r{_uid()}", [package_yaml(dep, "1.0.0")])
        _, _, _, info = await PackageService(db).preview(
            package_yaml(root, requires=[{"name": dep, "version": ">=2"}]), str(admin_user.id)
        )
        entry = _deps(info)[dep]
        assert entry["status"] == "missing" and "1.0.0 in" in entry["message"]

    async def test_incompatible_installed_version_is_refused(self, db, admin_user, fake_repos):
        dep, root = f"gmail{_uid()}", f"triage{_uid()}"
        service = PackageService(db)
        await service.install(package_yaml(dep, "1.0.0"), str(admin_user.id))
        # A compatible version is published, but an installed dependency is
        # never upgraded behind the operator's back.
        await fake_repos.add(db, admin_user, f"r{_uid()}", [package_yaml(dep, "2.0.0")])
        root_yaml = package_yaml(root, requires=[{"name": dep, "version": ">=2"}])
        result, _, _, info = await service.preview(root_yaml, str(admin_user.id))
        entry = _deps(info)[dep]
        assert not result.success and entry["status"] == "incompatible" and "update it first" in entry["message"]
        with pytest.raises(ValueError, match="update it first"):
            await service.install(root_yaml, str(admin_user.id))

    async def test_an_install_name_holding_another_package_is_incompatible(self, db, admin_user, fake_repos):
        other, root = f"other{_uid()}", f"triage{_uid()}"
        await PackageService(db).install(package_yaml(other), str(admin_user.id))
        _, _, _, info = await PackageService(db).preview(
            package_yaml(root, requires=[{"name": f"gmail{_uid()}", "instance": other}]), str(admin_user.id)
        )
        (entry,) = info["dependencies"]
        assert entry["status"] == "incompatible" and f"package '{other}'" in entry["message"]

    async def test_recursive_resolution_installs_in_order(self, db, admin_user, fake_repos):
        a, b, c = f"a{_uid()}", f"b{_uid()}", f"c{_uid()}"
        await fake_repos.add(db, admin_user, f"r{_uid()}", [
            package_yaml(b, requires=[{"name": c}], uses_skills=[f"{c}/hello"]),
            package_yaml(c),
        ])
        root_yaml = package_yaml(a, requires=[{"name": b}], uses_skills=[f"{b}/hello"])
        service = PackageService(db)
        result, _, _, info = await service.preview(root_yaml, str(admin_user.id))
        assert result.success, result.errors
        assert [d["install_name"] for d in info["dependencies"]] == [c, b]   # c before b
        assert _deps(info)[b]["requires"] == [c] and _deps(info)[c]["required_by"] == [b]

        await service.install(root_yaml, str(admin_user.id))
        assert [p.name for p, _ in service.installed_dependencies] == [c, b]

    async def test_diamond_resolves_once(self, db, admin_user, fake_repos):
        a, b, c, d = (f"{x}{_uid()}" for x in "abcd")
        await fake_repos.add(db, admin_user, f"r{_uid()}", [
            package_yaml(b, requires=[{"name": d, "version": ">=1"}]),
            package_yaml(c, requires=[{"name": d, "version": "<2"}]),
            package_yaml(d, "1.5.0"),
        ])
        _, _, _, info = await PackageService(db).preview(
            package_yaml(a, requires=[{"name": b}, {"name": c}]), str(admin_user.id)
        )
        assert [x["install_name"] for x in info["dependencies"]] == [d, b, c]
        assert _deps(info)[d]["required_by"] == sorted([b, c])

    async def test_diamond_with_conflicting_ranges(self, db, admin_user, fake_repos):
        a, b, c, d = (f"{x}{_uid()}" for x in "abcd")
        await fake_repos.add(db, admin_user, f"r{_uid()}", [
            package_yaml(b, requires=[{"name": d, "version": ">=1"}]),
            package_yaml(c, requires=[{"name": d, "version": ">=2"}]),
            package_yaml(d, "1.5.0"),
        ])
        result, _, _, info = await PackageService(db).preview(
            package_yaml(a, requires=[{"name": b}, {"name": c}]), str(admin_user.id)
        )
        entry = _deps(info)[d]
        # No version meets both: refused, naming both constraints and who set them.
        assert not result.success and entry["status"] == "missing"
        assert f"'{b}' requires {d} >=1" in entry["message"] and f"'{c}' requires {d} >=2" in entry["message"]

    async def test_a_shared_dependency_meets_every_constraint(self, db, admin_user, fake_repos):
        """The first constraint seen doesn't lock the version: every
        constraint is collected first (Greptile's two cases), whatever the
        order the requirements are listed in."""
        tag = _uid()
        b, c, d = f"b{tag}", f"c{tag}", f"d{tag}"
        preferred = await fake_repos.add(db, admin_user, f"a{tag}", [package_yaml(d, "1.5.0")])
        other = await fake_repos.add(db, admin_user, f"z{tag}", [
            package_yaml(d, "2.5.0"),
            # b looks in the preferred repository first.
            package_yaml(b, requires=[{"name": d, "version": ">=1", "repository": preferred.name}]),
            package_yaml(c, requires=[{"name": d, "version": ">=2"}]),
        ])
        service = PackageService(db)

        async def plan(order):
            result, _, _, info = await service.preview(
                package_yaml(f"root{tag}", requires=[{"name": n} for n in order]), str(admin_user.id)
            )
            return result, {e["install_name"]: (e["status"], e["version"], e["repository"]) for e in info["dependencies"]}

        result, first = await plan([b, c])
        assert result.success, result.errors
        assert first[d] == ("install", "2.5.0", other.name)   # >=1 alone would have taken 1.5.0
        _result, second = await plan([c, b])
        assert second == first

    async def test_a_later_upper_bound_picks_a_lower_version(self, db, admin_user, fake_repos):
        tag = _uid()
        b, c, d = f"b{tag}", f"c{tag}", f"d{tag}"
        newest = await fake_repos.add(db, admin_user, f"a{tag}", [package_yaml(d, "3.0.0")])
        older = await fake_repos.add(db, admin_user, f"z{tag}", [
            package_yaml(d, "1.5.0"),
            package_yaml(b, requires=[{"name": d, "version": ">=1", "repository": newest.name}]),
            package_yaml(c, requires=[{"name": d, "version": "<2"}]),
        ])
        for order in ([b, c], [c, b]):
            result, _, _, info = await PackageService(db).preview(
                package_yaml(f"root{tag}", requires=[{"name": n} for n in order]), str(admin_user.id)
            )
            assert result.success, result.errors
            assert (_deps(info)[d]["version"], _deps(info)[d]["repository"]) == ("1.5.0", older.name)

    async def test_an_installed_dependency_must_meet_every_constraint(self, db, admin_user, fake_repos):
        tag = _uid()
        b, c, d = f"b{tag}", f"c{tag}", f"d{tag}"
        await PackageService(db).install(package_yaml(d, "1.5.0"), str(admin_user.id))
        await fake_repos.add(db, admin_user, f"r{tag}", [
            package_yaml(d, "2.5.0"),   # available, but an installed one is never upgraded
            package_yaml(b, requires=[{"name": d, "version": ">=1"}]),
            package_yaml(c, requires=[{"name": d, "version": ">=2"}]),
        ])
        result, _, _, info = await PackageService(db).preview(
            package_yaml(f"root{tag}", requires=[{"name": b}, {"name": c}]), str(admin_user.id)
        )
        entry = _deps(info)[d]
        assert not result.success and entry["status"] == "incompatible"
        assert f"'{c}' requires {d} >=2" in entry["message"] and f"'{b}'" not in entry["message"]

    async def test_a_nested_requirement_waiting_for_a_variable(self, db, admin_user, fake_repos):
        """a requires b (to install); b requires mail under an instance its
        own variable names. First preview: b asks for MAILBOX; with it, mail
        is resolved and installed under that name."""
        tag = _uid()
        a, b, mail = f"a{tag}", f"b{tag}", f"mail{tag}"
        await fake_repos.add(db, admin_user, f"r{tag}", [
            package_yaml(mail, multi=True),
            package_yaml(b, requires=[{"name": mail, "instance": "${{ vars.MAILBOX }}"}], variable="MAILBOX"),
        ])
        root_yaml = package_yaml(a, requires=[{"name": b}])
        service = PackageService(db)

        result, _, _, info = await service.preview(root_yaml, str(admin_user.id))
        entry = _deps(info)[b]
        assert (entry["status"], entry["needs_input"], entry["requires_input"]) == ("install", True, True)
        assert [v["name"] for v in entry["variables"]] == ["MAILBOX"] and entry["requires"] == []
        assert not result.success and any("MAILBOX" in e for e in result.errors)
        savepoint = await db.begin_nested()
        with pytest.raises(ValueError, match="MAILBOX"):
            await service.install(root_yaml, str(admin_user.id))
        await savepoint.rollback()

        values = {b: {"MAILBOX": f"{mail}-support"}}
        result, _, _, info = await service.preview(root_yaml, str(admin_user.id), dependency_variables=values)
        assert result.success, result.errors
        assert [d["install_name"] for d in info["dependencies"]] == [f"{mail}-support", b]
        assert not _deps(info)[b]["needs_input"] and _deps(info)[b]["requires"] == [f"{mail}-support"]

        await service.install(root_yaml, str(admin_user.id), dependency_variables=values)
        assert (await _installed(db, f"{mail}-support")).package_name == mail

        # A value that still doesn't make a valid requirement: invalid.
        bad = {b: {"MAILBOX": "Not A Name"}}
        other_root = package_yaml(f"other{tag}", requires=[{"name": f"b2{tag}"}])
        await fake_repos.add(db, admin_user, f"s{tag}", [
            package_yaml(f"b2{tag}", requires=[{"name": mail, "instance": "${{ vars.MAILBOX }}"}], variable="MAILBOX"),
        ])
        _, _, _, info = await service.preview(
            other_root, str(admin_user.id), dependency_variables={f"b2{tag}": bad[b]}
        )
        entry = _deps(info)[f"b2{tag}"]
        assert entry["status"] == "invalid"
        # Its variables are still listed, so the value can be corrected...
        assert [v["name"] for v in entry["variables"]] == ["MAILBOX"] and entry["requires_input"]
        # ...and a corrected value resolves it.
        result, _, _, info = await service.preview(
            other_root, str(admin_user.id), dependency_variables={f"b2{tag}": {"MAILBOX": f"{mail}-other"}}
        )
        assert result.success, result.errors
        assert _deps(info)[f"b2{tag}"]["status"] == "install"

    async def test_a_cycle_through_installed_packages_is_refused(self, db, admin_user, fake_repos):
        """Upgrading a to require installed b, while b requires a: both would
        block each other's uninstall."""
        a, b = f"a{_uid()}", f"b{_uid()}"
        service = PackageService(db)
        await service.install(package_yaml(a, "1.0.0"), str(admin_user.id))
        await service.install(package_yaml(b, requires=[{"name": a}]), str(admin_user.id))
        upgrade = package_yaml(a, "2.0.0", requires=[{"name": b}])

        result, _, _, info = await service.preview(upgrade, str(admin_user.id))
        assert not result.success and _deps(info)[b]["status"] == "cycle"
        assert f"{a} → {b} → {a}" in _deps(info)[b]["message"]
        with pytest.raises(ValueError, match=f"{a} → {b} → {a}"):
            await service.install(upgrade, str(admin_user.id))
        assert (await _installed(db, a)).version == "1.0.0"

    async def test_cycles_fail_clearly(self, db, admin_user, fake_repos):
        a, b, c = f"a{_uid()}", f"b{_uid()}", f"c{_uid()}"
        await fake_repos.add(db, admin_user, f"r{_uid()}", [
            package_yaml(b, requires=[{"name": c}]),
            package_yaml(c, requires=[{"name": b}]),
            package_yaml(a, "2.0.0"),
        ])
        result, _, _, info = await PackageService(db).preview(
            package_yaml(a, requires=[{"name": b}]), str(admin_user.id)
        )
        assert not result.success
        assert any(f"{b} → {c} → {b}" in e for e in result.errors), result.errors

        # Back to the package being installed.
        b2 = f"b{_uid()}"
        await fake_repos.add(db, admin_user, f"r{_uid()}", [package_yaml(b2, requires=[{"name": a}])])
        result, _, _, info = await PackageService(db).preview(
            package_yaml(a, requires=[{"name": b2}]), str(admin_user.id)
        )
        assert any(f"{a} → {b2} → {a}" in e for e in result.errors), result.errors
        with pytest.raises(ValueError, match="circular"):
            await PackageService(db).install(package_yaml(a, requires=[{"name": b2}]), str(admin_user.id))


# ---------------------------------------------------------------- install semantics


class TestInstall:
    async def test_a_failing_package_leaves_no_dependency_behind(self, db, admin_user, fake_repos):
        """One transaction: the dependency installed first is rolled back with
        the package that failed (the API's session rolls back on the error;
        a savepoint stands in for it here)."""
        dep, root = f"gmail{_uid()}", f"triage{_uid()}"
        await fake_repos.add(db, admin_user, f"r{_uid()}", [package_yaml(dep)])
        # The root's agent refers to a skill nobody declares: its apply fails
        # after the dependency was applied.
        root_yaml = package_yaml(root, requires=[{"name": dep}], uses_skills=[f"{dep}/nothing"])
        savepoint = await db.begin_nested()
        with pytest.raises(ValueError, match="nothing was applied"):
            await PackageService(db).install(root_yaml, str(admin_user.id))
        await savepoint.rollback()
        assert await _installed(db, dep) is None and await _installed(db, root) is None
        assert await Skill.get_by_name(db, dep, "hello") is None

    async def test_a_failing_dependency_is_named(self, db, admin_user, fake_repos):
        dep, root = f"gmail{_uid()}", f"triage{_uid()}"
        await fake_repos.add(db, admin_user, f"r{_uid()}", [package_yaml(dep, uses_skills=["nobody/nothing"])])
        savepoint = await db.begin_nested()
        with pytest.raises(ValueError, match=f"Required package '{dep}'.*failed to install"):
            await PackageService(db).install(package_yaml(root, requires=[{"name": dep}]), str(admin_user.id))
        await savepoint.rollback()
        assert await _installed(db, dep) is None

    async def test_a_plan_that_did_not_settle_is_refused(self, db, admin_user, fake_repos, monkeypatch):
        """A resolve that hit its round limit reports a problem without any
        one entry being blocked; the install must refuse it as the preview does."""
        from app.services import package_dependencies as deps

        async def unsettled(*args, **kwargs):
            return deps.Plan(problems=["Requirements did not settle after 50 rounds"])

        monkeypatch.setattr(deps, "resolve_plan", unsettled)
        root = f"triage{_uid()}"
        with pytest.raises(ValueError, match="did not settle"):
            await PackageService(db).install(package_yaml(root, requires=[{"name": "anything"}]), str(admin_user.id))
        assert await _installed(db, root) is None

    async def test_dependency_variables(self, db, admin_user, fake_repos):
        dep, root = f"gmail{_uid()}", f"triage{_uid()}"
        await fake_repos.add(db, admin_user, f"r{_uid()}", [package_yaml(dep, variable="GREETING")])
        root_yaml = package_yaml(root, requires=[{"name": dep}], uses_skills=[f"{dep}/hello"])
        service = PackageService(db)

        result, _, _, info = await service.preview(root_yaml, str(admin_user.id))
        entry = _deps(info)[dep]
        assert entry["requires_input"] and [v["name"] for v in entry["variables"]] == ["GREETING"]
        assert not result.success
        # What it lacks is the dependency's values, not the skill it will provide.
        assert not any(f"{dep}/hello" in e for e in result.errors), result.errors

        values = {dep: {"GREETING": "Hi there."}}
        result, _, _, info = await service.preview(root_yaml, str(admin_user.id), dependency_variables=values)
        assert result.success, result.errors
        await service.install(root_yaml, str(admin_user.id), dependency_variables=values)
        assert (await Skill.get_by_name(db, dep, "hello")).content == "Hi there."
        assert (await _installed(db, dep)).values == {"GREETING": "Hi there."}


# ---------------------------------------------------------------- uninstall and upgrades


class TestUninstallAndUpgrade:
    async def test_uninstall_is_refused_while_required(self, client, db, admin_user, fake_repos):
        dep, root = f"gmail{_uid()}", f"triage{_uid()}"
        await fake_repos.add(db, admin_user, f"r{_uid()}", [package_yaml(dep)])
        service = PackageService(db)
        await service.install(package_yaml(root, requires=[{"name": dep}]), str(admin_user.id))

        with pytest.raises(PackageInUse) as refused:
            await service.uninstall(dep)
        assert refused.value.dependents == [root]
        h = auth_headers(admin_user)
        r = await client.delete(f"/api/v1/packages/{dep}", headers=h)
        assert r.status_code == 409 and r.json()["detail"]["required_by"] == [root]

        shown = (await client.get(f"/api/v1/packages/{dep}", headers=h)).json()
        assert shown["required_by"] == [root] and shown["installed_as_dependency"] and not shown["orphaned"]
        shown_root = (await client.get(f"/api/v1/packages/{root}", headers=h)).json()
        assert [(q["name"], q["status"]) for q in shown_root["requires"]] == [(dep, "satisfied")]

        # Dependents first; the dependency is then an orphan, not removed.
        assert (await client.delete(f"/api/v1/packages/{root}", headers=h)).status_code == 204
        listed = {p["name"]: p for p in (await client.get("/api/v1/packages", headers=h)).json()}
        assert listed[dep]["orphaned"] and listed[dep]["required_by"] == []
        assert (await client.delete(f"/api/v1/packages/{dep}", headers=h)).status_code == 204

    async def test_an_upgrade_that_breaks_a_dependent_is_refused_unless_forced(self, db, admin_user, fake_repos):
        dep, root = f"gmail{_uid()}", f"triage{_uid()}"
        service = PackageService(db)
        await service.install(package_yaml(dep, "1.0.0"), str(admin_user.id))
        await service.install(package_yaml(root, requires=[{"name": dep, "version": "<2"}]), str(admin_user.id))

        new = package_yaml(dep, "2.0.0")
        result, _, _, info = await service.preview(new, str(admin_user.id))
        assert not result.success and info["breaks"] == [{"dependent": root, "requires": dep, "version": "<2"}]
        with pytest.raises(ValueError, match="would break"):
            await service.install(new, str(admin_user.id))
        assert (await _installed(db, dep)).version == "1.0.0"

        # Within the range is fine; forcing is the only way past it.
        await service.install(package_yaml(dep, "1.5.0"), str(admin_user.id))
        _package, forced = await service.install(new, str(admin_user.id), allow_breaking_dependents=True)
        assert any("allowBreakingDependents" in w for w in forced.warnings)
        assert (await _installed(db, dep)).version == "2.0.0"

    async def test_preview_and_install_check_the_substituted_version(self, db, admin_user, fake_repos):
        """version: ${{ vars.VERSION }}: dependents are checked against the
        version it is installed as, in the preview and the install alike."""
        dep, root = f"gmail{_uid()}", f"triage{_uid()}"
        service = PackageService(db)
        templated = package_yaml(dep, "${{ vars.VERSION }}", variable="VERSION")
        await service.install(templated, str(admin_user.id), variables={"VERSION": "1.0.0"})
        await service.install(package_yaml(root, requires=[{"name": dep, "version": "<2"}]), str(admin_user.id))

        result, _, _, info = await service.preview(templated, str(admin_user.id), variables={"VERSION": "2.0.0"})
        assert not result.success and [b["dependent"] for b in info["breaks"]] == [root]
        with pytest.raises(ValueError, match="would break"):
            await service.install(templated, str(admin_user.id), variables={"VERSION": "2.0.0"})

        result, _, _, info = await service.preview(templated, str(admin_user.id), variables={"VERSION": "1.5.0"})
        assert result.success and info["breaks"] == [], result.errors
        await service.install(templated, str(admin_user.id), variables={"VERSION": "1.5.0"})
        assert (await _installed(db, dep)).version == "1.5.0"

    async def test_an_empty_version_still_breaks_dependents(self, db, admin_user, fake_repos):
        """version: "" (or an empty value for its variable) is no version: it
        satisfies no range, so dependents are still protected."""
        dep, root = f"gmail{_uid()}", f"triage{_uid()}"
        service = PackageService(db)
        await service.install(package_yaml(dep), str(admin_user.id))
        await service.install(package_yaml(root, requires=[{"name": dep, "version": ">=1,<2"}]), str(admin_user.id))

        empty = package_yaml(dep, "")
        result, _, _, info = await service.preview(empty, str(admin_user.id))
        assert not result.success and [b["dependent"] for b in info["breaks"]] == [root]
        with pytest.raises(ValueError, match="would break"):
            await service.install(empty, str(admin_user.id))
        assert (await _installed(db, dep)).version == "1.0.0"

    async def test_an_upgrade_rechecks_its_own_requirements(self, db, admin_user, fake_repos):
        dep, root = f"gmail{_uid()}", f"triage{_uid()}"
        await fake_repos.add(db, admin_user, f"r{_uid()}", [package_yaml(dep)])
        service = PackageService(db)
        await service.install(package_yaml(root, "1.0.0"), str(admin_user.id))
        await service.install(package_yaml(root, "1.1.0", requires=[{"name": dep}]), str(admin_user.id))
        assert [p.name for p, _ in service.installed_dependencies] == [dep]
        assert (await _installed(db, root)).requires[0]["name"] == dep


# ---------------------------------------------------------------- multi-instance


class TestMultiInstance:
    async def test_a_requirement_names_the_instance(self, db, admin_user, fake_repos):
        mail, root = f"mail{_uid()}", f"triage{_uid()}"
        await fake_repos.add(db, admin_user, f"r{_uid()}", [package_yaml(mail, multi=True)])
        instance = f"{mail}-support"
        service = PackageService(db)
        await service.install(
            package_yaml(root, requires=[{"name": mail, "instance": instance}], uses_skills=[f"{instance}/hello"]),
            str(admin_user.id),
        )
        row = await _installed(db, instance)
        assert row.package_name == mail and row.installed_as_dependency
        assert await _installed(db, mail) is None

    async def test_the_instance_can_come_from_a_variable(self, db, admin_user, fake_repos):
        mail, root = f"mail{_uid()}", f"triage{_uid()}"
        service = PackageService(db)
        await service.install(package_yaml(mail, multi=True), str(admin_user.id), instance=f"{mail}-work")
        root_yaml = package_yaml(root, requires=[{"name": mail, "instance": "${{ vars.MAILBOX }}"}],
                                 variable="MAILBOX")
        result, _, _, info = await service.preview(root_yaml, str(admin_user.id), variables={"MAILBOX": f"{mail}-work"})
        assert result.success, result.errors
        assert _deps(info)[f"{mail}-work"]["status"] == "satisfied"

    async def test_an_instance_of_a_single_instance_package_is_invalid(self, db, admin_user, fake_repos):
        single, root = f"single{_uid()}", f"triage{_uid()}"
        await fake_repos.add(db, admin_user, f"r{_uid()}", [package_yaml(single)])
        result, _, _, info = await PackageService(db).preview(
            package_yaml(root, requires=[{"name": single, "instance": f"{single}-two"}]), str(admin_user.id)
        )
        (entry,) = info["dependencies"]
        assert not result.success and entry["status"] == "invalid" and "multiple instances" in entry["message"]


# ---------------------------------------------------------------- references


class TestReferences:
    async def test_undeclared_references_keep_todays_behaviour(self, db, admin_user, fake_repos):
        """Without requires: a reference to another package's resource works
        only if it is installed, and uninstalling it is not blocked."""
        other, user = f"other{_uid()}", f"user{_uid()}"
        await fake_repos.add(db, admin_user, f"r{_uid()}", [package_yaml(other)])  # available, but not declared
        service = PackageService(db)
        user_yaml = package_yaml(user, uses_skills=[f"{other}/hello"])

        result, _, _, info = await service.preview(user_yaml, str(admin_user.id))
        assert not result.success and info["dependencies"] == []
        assert any(f"{other}/hello" in e for e in result.errors)

        await service.install(package_yaml(other), str(admin_user.id))
        await service.install(user_yaml, str(admin_user.id))
        await service.uninstall(other)   # not declared: not protected
        assert await _installed(db, other) is None

    async def test_a_reference_to_an_undeclared_resource_of_a_dependency_fails(self, db, admin_user, fake_repos):
        dep, root = f"gmail{_uid()}", f"triage{_uid()}"
        await fake_repos.add(db, admin_user, f"r{_uid()}", [package_yaml(dep)])
        result, _, _, _ = await PackageService(db).preview(
            package_yaml(root, requires=[{"name": dep}], uses_skills=[f"{dep}/other"]), str(admin_user.id)
        )
        assert not result.success and any(f"{dep}/other" in e for e in result.errors)

    async def test_manifest_requirements_count_what_dependencies_provide(self, db, admin_user, fake_repos):
        """A manifest whose required resource exists nowhere is refused (as
        before); one the planned dependency declares counts, in the preview
        (nothing created yet) and the install (installed first)."""
        dep, root = f"gmail{_uid()}", f"triage{_uid()}"
        await fake_repos.add(db, admin_user, f"r{_uid()}", [package_yaml(dep)])
        provided = [
            {"type": "skill", "namespace": dep, "name": "hello"},      # the dependency's
            {"type": "skill", "namespace": root, "name": "hello"},     # its own
        ]
        root_yaml = package_yaml(root, requires=[{"name": dep}], manifest_requires=provided)
        service = PackageService(db)
        result, _, _, _ = await service.preview(root_yaml, str(admin_user.id))
        assert result.success, result.errors

        # A connector nobody declares: refused, now that manifests may name connectors.
        broken = package_yaml(root, requires=[{"name": dep}], manifest_requires=provided + [
            {"type": "connector", "namespace": dep, "name": "api"},
        ])
        with pytest.raises(ValueError, match=f"connector '{dep}/api' not defined"):
            await service.preview(broken, str(admin_user.id))

        await service.install(root_yaml, str(admin_user.id))
        assert await _installed(db, root) is not None

    async def test_manifest_status_knows_more_resource_types(self, db, admin_user):
        from app.models.manifest import Manifest
        from app.services.manifest_validator import validate_manifest_status

        pkg = f"conn{_uid()}"
        await PackageService(db).install(
            "apiVersion: sinas.co/v1\nkind: SinasPackage\n"
            f"package: {{name: {pkg}}}\n"
            f"spec:\n  connectors:\n    - {{namespace: {pkg}, name: api, baseUrl: 'https://example.com'}}\n",
            str(admin_user.id),
        )
        manifest = Manifest(namespace=pkg, name="m", user_id=admin_user.id, required_resources=[
            {"type": "connector", "namespace": pkg, "name": "api"},
            {"type": "connectors", "namespace": pkg, "name": "nope"},
        ])
        status = await validate_manifest_status(db, manifest, str(admin_user.id), {})
        assert [r.name for r in status.resources["satisfied"]] == ["api"]
        assert [r.name for r in status.resources["missing"]] == ["nope"]


# ---------------------------------------------------------------- repositories


class TestRepositories:
    def test_the_index_keeps_requires_and_drops_bad_ones(self):
        entry = {"name": "x", "version": "1.0.0", "path": "p/x.yaml", "sha256": "a" * 64, "requires": [
            {"name": "gmail", "version": ">=1.2,<2", "repository": "sinas-official"},
            {"name": "Bad Name"},
            {"name": "slack", "version": "not a range"},
            "gmail",
        ]}
        index = repos.parse_index(json.dumps({"schema": repos.INDEX_SCHEMA, "packages": [entry]}).encode())
        assert index["packages"][0]["requires"] == [
            {"name": "gmail", "version": ">=1.2,<2", "repository": "sinas-official"},
        ]
        bare = {k: v for k, v in entry.items() if k != "requires"}
        index = repos.parse_index(json.dumps({"schema": repos.INDEX_SCHEMA, "packages": [bare]}).encode())
        assert index["packages"][0]["requires"] == []

    async def test_the_catalog_shows_requires_with_their_status(self, client, db, admin_user, fake_repos):
        """Met only when the install name holds the required package at a
        version in range."""
        ok, old, other, absent, root = (f"{x}{_uid()}" for x in ("ok", "old", "other", "absent", "triage"))
        service = PackageService(db)
        await service.install(package_yaml(ok, "1.5.0"), str(admin_user.id))
        await service.install(package_yaml(old, "1.0.0"), str(admin_user.id))
        await service.install(package_yaml(other), str(admin_user.id))
        requires = [
            {"name": ok, "version": ">=1"},
            {"name": old, "version": ">=2"},
            {"name": f"mail{_uid()}", "instance": other},   # the name holds another package
            {"name": absent},
        ]
        await fake_repos.add(db, admin_user, f"r{_uid()}", [package_yaml(root, requires=requires)],
                             extra={root: {"requires": requires}})
        catalog = (await client.get("/api/v1/packages/catalog", headers=auth_headers(admin_user))).json()
        (entry,) = [e for e in catalog if e["name"] == root]
        got = [(r["install_name"], r["status"], r["reason"], r["installed_version"]) for r in entry["requires"]]
        assert got == [
            (ok, "satisfied", None, "1.5.0"),
            (old, "incompatible", "version", "1.0.0"),
            (other, "incompatible", "other_package", "1.0.0"),
            (absent, "missing", None, None),
        ]
        assert entry["requires"][0]["version"] == ">=1" and entry["requires"][2]["installed_package"] == other

    async def test_resolution_order(self, db, admin_user, fake_repos):
        """The requirement's hint, then the dependent's own repository, then
        the others by name — and only a version in range counts."""
        dep, root = f"gmail{_uid()}", f"triage{_uid()}"
        tag = _uid()
        first = await fake_repos.add(db, admin_user, f"a{tag}", [package_yaml(dep, "1.0.0")])
        own = await fake_repos.add(db, admin_user, f"b{tag}", [package_yaml(dep, "1.1.0")])
        hinted = await fake_repos.add(db, admin_user, f"c{tag}", [package_yaml(dep, "1.2.0")])
        service = PackageService(db)

        async def chosen(requirement, repository=None):
            _, _, _, info = await service.preview(
                package_yaml(root, requires=[requirement]), str(admin_user.id), repository=repository
            )
            return _deps(info)[dep]["repository"]

        assert await chosen({"name": dep}) == first.name
        assert await chosen({"name": dep}, repository=own.name) == own.name
        assert await chosen({"name": dep, "repository": hinted.name}, repository=own.name) == hinted.name
        # The hint's version is out of range: the next one in order that fits.
        assert await chosen({"name": dep, "version": "<1.2", "repository": hinted.name}, repository=own.name) == own.name
        assert await chosen({"name": dep, "version": "<1.1", "repository": hinted.name}, repository=own.name) == first.name

    async def test_each_dependency_file_is_checksum_verified(self, db, admin_user, fake_repos):
        dep, root = f"gmail{_uid()}", f"triage{_uid()}"
        repo = await fake_repos.add(db, admin_user, f"r{_uid()}", [package_yaml(dep)])
        fake_repos.files[f"{repo.url}packages/{dep}/package.yaml"] += b"# tampered\n"
        _, _, _, info = await PackageService(db).preview(
            package_yaml(root, requires=[{"name": dep}]), str(admin_user.id)
        )
        entry = _deps(info)[dep]
        assert entry["status"] == "missing" and "checksum" in entry["message"]

    async def test_install_through_the_api(self, client, db, admin_user, fake_repos):
        dep, root = f"gmail{_uid()}", f"triage{_uid()}"
        repo = await fake_repos.add(db, admin_user, f"r{_uid()}", [
            package_yaml(dep, "1.3.0", variable="GREETING"),
            package_yaml(root, requires=[{"name": dep}], uses_skills=[f"{dep}/hello"]),
        ])
        h = auth_headers(admin_user)
        body = {"repository": repo.name, "package": root, "dependencyVariables": {dep: {"GREETING": "Hello"}}}
        preview = (await client.post("/api/v1/packages/preview", json=body, headers=h)).json()
        assert preview["success"], preview["errors"]
        (entry,) = preview["dependencies"]
        assert (entry["install_name"], entry["status"], entry["version"]) == (dep, "install", "1.3.0")
        r = await client.post("/api/v1/packages/install", json=body, headers=h)
        assert r.status_code == 200, r.text
        installed = r.json()
        assert installed["package"]["name"] == root
        assert [d["package"]["name"] for d in installed["dependencies"]] == [dep]
        assert installed["dependencies"][0]["package"]["installed_as_dependency"]
