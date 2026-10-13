"""Package repositories (ADR 2026-10-13-package-repositories).

A repository serves an index.json listing its packages; Sinas refreshes it,
offers its packages as a catalog, installs from it (checking each file
against the index's checksum), and reports when an installed package has a
newer version published. The repository here is served by an httpx
MockTransport: no network.
"""
import contextlib
import hashlib
import json
import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.config_revision import ConfigRevision
from app.models.package import Package
from app.models.package_repository import PackageRepository
from app.schemas.config import SinasConfig
from app.services import package_repositories as repos
from app.services.config_apply.service import ConfigApplyService
from app.services.package_repositories import RepositoryError, base_url, file_url, is_newer, parse_index
from tests.conftest import auth_headers

REPO_URL = "https://github.com/acme/packages"
RAW = "https://raw.githubusercontent.com/acme/packages/main/"


def _package(name: str, version: str) -> str:
    return (
        "apiVersion: sinas.co/v1\nkind: SinasPackage\n"
        f"package:\n  name: {name}\n  version: \"{version}\"\n  description: A demo\n"
        "spec:\n  skills:\n"
        f"    - namespace: {name}\n      name: hello\n      description: Says hello\n      content: Say hello.\n"
    )


class FakeRepository:
    """index.json and package files under RAW, as a MockTransport serves them."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.requests: list[str] = []
        self.headers: list[dict] = []  # per request
        self.redirects: dict[str, str] = {}  # full URL -> Location
        self.down = False

    def publish(self, name: str, version: str, *, tamper: bool = False, declares: str | None = None,
                readme: str | None = None, body: str | None = None, extra: dict | None = None) -> None:
        raw = (body if body is not None else _package(declares or name, version)).encode()
        self.files[f"packages/{name}/package.yaml"] = raw + (b"# changed\n" if tamper else b"")
        entries = [e for e in self._index()["packages"] if e["name"] != name]
        entries.append({
            "name": name, "version": version, "description": "A demo", "author": "Acme",
            "path": f"packages/{name}/package.yaml", "sha256": hashlib.sha256(raw).hexdigest(),
            **({"readme": readme} if readme else {}),
            **(extra or {}),
        })
        self.files["index.json"] = json.dumps({"schema": repos.INDEX_SCHEMA, "packages": entries}).encode()

    def _index(self) -> dict:
        return json.loads(self.files.get("index.json", b'{"packages": []}'))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        self.headers.append(dict(request.headers))
        if str(request.url) in self.redirects:
            return httpx.Response(302, headers={"location": self.redirects[str(request.url)]})
        if self.down:
            return httpx.Response(503)
        path = str(request.url).removeprefix(RAW)
        if path in self.files:
            return httpx.Response(200, content=self.files[path])
        return httpx.Response(404)


@pytest.fixture
def fake_repo(monkeypatch):
    fake = FakeRepository()
    real = httpx.AsyncClient
    monkeypatch.setattr(
        "app.services.package_repositories.httpx.AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(fake.handler), **kw),
    )
    return fake


def _uid() -> str:
    return uuid.uuid4().hex[:8]


# ---------------------------------------------------------------- pure parts


class TestUrls:
    @pytest.mark.parametrize("url, base", [
        ("https://github.com/acme/packages", RAW),
        ("https://github.com/acme/packages.git", RAW),
        ("https://github.com/acme/packages/tree/dev/sub/dir", "https://raw.githubusercontent.com/acme/packages/dev/sub/dir/"),
        ("https://pkgs.example.com/sinas", "https://pkgs.example.com/sinas/"),
        ("https://pkgs.example.com/sinas/index.json", "https://pkgs.example.com/sinas/"),
    ])
    def test_base_urls(self, url, base):
        assert base_url(url) == base

    @pytest.mark.parametrize("url", [
        "http://pkgs.example.com/", "ftp://pkgs.example.com/", "https://user:token@pkgs.example.com/",
        "https://github.com/acme",
    ])
    def test_refused_urls(self, url):
        with pytest.raises(RepositoryError):
            base_url(url)

    @pytest.mark.parametrize("path", ["../secrets.yaml", "/etc/passwd", "a/../../b", "https://evil.example/x", "a\\b", ""])
    def test_package_paths_stay_inside_the_repository(self, path):
        with pytest.raises(RepositoryError):
            file_url(REPO_URL, path)

    def test_a_package_path_resolves_against_the_base(self):
        assert file_url(REPO_URL, "packages/x/package.yaml") == RAW + "packages/x/package.yaml"


class TestIndex:
    def _entry(self, **over):
        return {"name": "x", "version": "1.0.0", "path": "packages/x/package.yaml", "sha256": "a" * 64, **over}

    def test_a_valid_index(self):
        index = parse_index(json.dumps({"schema": repos.INDEX_SCHEMA, "packages": [self._entry()]}).encode())
        assert [p["name"] for p in index["packages"]] == ["x"]

    @pytest.mark.parametrize("data", [
        b"not json",
        json.dumps({"schema": "other/v1", "packages": []}).encode(),
        json.dumps({"schema": repos.INDEX_SCHEMA, "packages": [{"name": "x"}]}).encode(),
        json.dumps({"schema": repos.INDEX_SCHEMA, "packages": [
            {"name": "x", "version": "1", "path": "p", "sha256": "a" * 64},
            {"name": "x", "version": "2", "path": "p", "sha256": "a" * 64},
        ]}).encode(),
        json.dumps({"schema": repos.INDEX_SCHEMA, "packages": [
            {"name": "x", "version": "1", "path": "p", "sha256": "nothex"},
        ]}).encode(),
    ])
    def test_invalid_indexes_are_refused(self, data):
        with pytest.raises(RepositoryError):
            parse_index(data)

    @pytest.mark.parametrize("value, expected", [
        (True, True),
        (False, False),
        (None, False),     # left out
        ("true", False),   # not a boolean: dropped
        (1, False),
        ({"x": 1}, False),
    ])
    def test_multi_instance_is_an_optional_boolean(self, value, expected):
        entry = self._entry() if value is None else self._entry(multiInstance=value)
        index = parse_index(json.dumps({"schema": repos.INDEX_SCHEMA, "packages": [entry]}).encode())
        assert index["packages"][0]["multi_instance"] is expected


def test_versions():
    assert is_newer("1.0.1", "1.0.0") and is_newer("1.10.0", "1.9.9") and is_newer("1.0.0", "1.0.0-rc.1")
    assert not is_newer("1.0.0", "1.0.0") and not is_newer("0.5.1", "0.10.0") and not is_newer("weird", "1.0.0")
    # Build metadata doesn't order versions.
    assert not is_newer("1.0.0", "1.0.0+build.1") and not is_newer("1.0.0+b2", "1.0.0+b1")
    assert is_newer("1.0.1+b1", "1.0.0+b9")
    # A hyphen stays inside a pre-release label, which then compares as text.
    assert is_newer("1.0.0-alpha-2", "1.0.0-alpha-10") and not is_newer("1.0.0-alpha-10", "1.0.0-alpha-2")
    assert is_newer("1.0.0-alpha.10", "1.0.0-alpha.2")


# ---------------------------------------------------------------- the API


async def _add(client, admin_user, name=None, url=REPO_URL):
    r = await client.post("/api/v1/package-repositories", json={"name": name or f"r-{_uid()}", "url": url},
                          headers=auth_headers(admin_user))
    assert r.status_code == 201, r.text
    return r.json()


class TestRepositories:
    async def test_adding_one_fetches_its_index(self, client, admin_user, fake_repo):
        fake_repo.publish("demo", "1.0.0")
        repo = await _add(client, admin_user)
        assert repo["package_count"] == 1 and repo["last_error"] is None and repo["last_refreshed_at"]
        assert fake_repo.requests == [RAW + "index.json"]

    async def test_a_failed_refresh_keeps_the_last_good_index(self, client, admin_user, fake_repo):
        fake_repo.publish("demo", "1.0.0")
        repo = await _add(client, admin_user)
        fake_repo.down = True
        r = await client.post(f"/api/v1/package-repositories/{repo['name']}/refresh", headers=auth_headers(admin_user))
        assert r.status_code == 200 and "503" in r.json()["last_error"] and r.json()["package_count"] == 1

    async def test_the_official_quick_add_is_idempotent(self, client, admin_user, monkeypatch, fake_repo):
        monkeypatch.setitem(repos.OFFICIAL_REPOSITORY, "url", REPO_URL)
        fake_repo.publish("demo", "1.0.0")
        h = auth_headers(admin_user)
        first = await client.post("/api/v1/package-repositories/official", headers=h)
        again = await client.post("/api/v1/package-repositories/official", headers=h)
        assert first.status_code == again.status_code == 200
        assert first.json()["id"] == again.json()["id"] and first.json()["name"] == "sinas-official"

    async def test_bad_urls_are_refused_up_front(self, client, admin_user):
        r = await client.post("/api/v1/package-repositories", json={"name": "bad", "url": "http://plain.example.com"},
                              headers=auth_headers(admin_user))
        assert r.status_code == 400 and "https" in r.json()["detail"]

    async def test_only_admins(self, client, test_user):
        h = auth_headers(test_user)
        assert (await client.get("/api/v1/package-repositories", headers=h)).status_code == 403
        assert (await client.post("/api/v1/package-repositories", json={"name": "x", "url": REPO_URL}, headers=h)).status_code == 403

    async def test_changes_are_in_the_change_history(self, client, db: AsyncSession, admin_user, fake_repo):
        fake_repo.publish("demo", "1.0.0")
        repo = await _add(client, admin_user)
        h = auth_headers(admin_user)
        assert (await client.put(f"/api/v1/package-repositories/{repo['name']}", json={"description": "d"}, headers=h)).status_code == 200
        assert (await client.delete(f"/api/v1/package-repositories/{repo['name']}", headers=h)).status_code == 204
        actions = (await db.execute(
            select(ConfigRevision.action)
            .where(ConfigRevision.resource_kind == "packageRepositories", ConfigRevision.resource_key == repo["name"])
            .order_by(ConfigRevision.id)
        )).scalars().all()
        assert list(actions) == ["create", "update", "delete"]


class TestHardening:
    async def test_a_redirect_to_plain_http_is_refused(self, client, admin_user, fake_repo):
        fake_repo.redirects[RAW + "index.json"] = "http://evil.example.com/index.json"
        repo = await _add(client, admin_user)
        assert "non-https" in repo["last_error"] and repo["package_count"] == 0
        assert "http://evil.example.com/index.json" not in fake_repo.requests

    async def test_credentials_never_follow_a_redirect_to_another_host(self, client, db, admin_user, fake_repo):
        from app.core.encryption import encryption_service
        from app.models.secret import Secret

        secret_name = f"TOK{_uid()}"
        db.add(Secret(name=secret_name, encrypted_value=encryption_service.encrypt("s3cret"),
                      user_id=admin_user.id, visibility="shared"))
        await db.flush()
        fake_repo.publish("demo", "1.0.0")
        fake_repo.redirects[RAW + "index.json"] = "https://cdn.example.net/index.json"
        r = await client.post("/api/v1/package-repositories", headers=auth_headers(admin_user), json={
            "name": f"r-{_uid()}", "url": REPO_URL, "auth": {"type": "header", "secret": secret_name, "header": "X-Token"},
        })
        assert r.status_code == 201, r.text
        first, second = fake_repo.headers[0], fake_repo.headers[1]
        assert first.get("x-token") == "s3cret"
        assert "x-token" not in second  # another host: no credentials

    async def test_a_refresh_that_finishes_after_a_repoint_is_dropped(self, client, db, admin_user, fake_repo, monkeypatch):
        fake_repo.publish("demo", "1.0.0")
        repo_name = (await _add(client, admin_user))["name"]
        real_get = repos._get

        async def slow_then_repointed(url, headers):
            data = await real_get(url, headers)
            row = (await db.execute(select(PackageRepository).where(PackageRepository.name == repo_name))).scalar_one()
            row.url = "https://pkgs.example.com/elsewhere"  # an update landed meanwhile
            row.index = None
            await db.flush()
            return data

        monkeypatch.setattr(repos, "_get", slow_then_repointed)
        row = (await db.execute(select(PackageRepository).where(PackageRepository.name == repo_name))).scalar_one()
        row.url = REPO_URL
        await repos.refresh(db, row)
        assert row.url == "https://pkgs.example.com/elsewhere" and row.index is None

    async def test_repositories_cannot_be_renamed(self, client, admin_user, fake_repo):
        fake_repo.publish("demo", "1.0.0")
        repo = await _add(client, admin_user)
        r = await client.put(f"/api/v1/package-repositories/{repo['name']}", json={"name": "other"},
                             headers=auth_headers(admin_user))
        assert r.status_code == 422

    async def test_the_catalog_says_which_packages_are_multi_instance(self, client, admin_user, fake_repo):
        multi, single, odd = f"multi{_uid()}", f"single{_uid()}", f"odd{_uid()}"
        fake_repo.publish(multi, "1.0.0", extra={"multiInstance": True})
        fake_repo.publish(single, "1.0.0")
        fake_repo.publish(odd, "1.0.0", extra={"multiInstance": "yes"})  # not a boolean: dropped
        repo = await _add(client, admin_user)
        assert repo["last_error"] is None and repo["package_count"] == 3
        catalog = await client.get("/api/v1/packages/catalog", headers=auth_headers(admin_user))
        flags = {e["name"]: e["multi_instance"] for e in catalog.json() if e["name"] in (multi, single, odd)}
        assert flags == {multi: True, single: False, odd: False}

    async def test_a_bad_readme_link_is_dropped_not_fatal(self, client, admin_user, fake_repo):
        name = f"demo{_uid()}"
        fake_repo.publish(name, "1.0.0", readme="/etc/passwd")
        await _add(client, admin_user)
        catalog = await client.get("/api/v1/packages/catalog", headers=auth_headers(admin_user))
        assert catalog.status_code == 200
        (entry,) = [e for e in catalog.json() if e["name"] == name]
        assert entry["readme_url"] is None

    async def test_a_file_declaring_another_package_is_refused(self, client, admin_user, fake_repo):
        name = f"demo{_uid()}"
        fake_repo.publish(name, "1.0.0", declares=f"other{_uid()}")
        repo = await _add(client, admin_user)
        r = await client.post("/api/v1/packages/install", json={"repository": repo["name"], "package": name},
                              headers=auth_headers(admin_user))
        assert r.status_code == 502 and "declares" in r.json()["detail"]

    @pytest.mark.parametrize("body", ["package: demo\n", "package: [1, 2]\n", "- just\n- a list\n"])
    async def test_a_file_without_package_metadata_is_refused(self, client, admin_user, fake_repo, body):
        name = f"demo{_uid()}"
        fake_repo.publish(name, "1.0.0")
        fake_repo.files[f"packages/{name}/package.yaml"] = body.encode()
        index = fake_repo._index()
        index["packages"][0]["sha256"] = hashlib.sha256(body.encode()).hexdigest()
        fake_repo.files["index.json"] = json.dumps(index).encode()
        repo = await _add(client, admin_user)
        r = await client.post("/api/v1/packages/install", json={"repository": repo["name"], "package": name},
                              headers=auth_headers(admin_user))
        assert r.status_code == 502 and "no package metadata" in r.json()["detail"]

    async def test_a_long_file_url_is_recorded_with_the_install(self, client, db, admin_user, fake_repo):
        name = f"demo{_uid()}"
        fake_repo.publish(name, "1.0.0")
        long_path = f"packages/{'d' * 1100}/{name}.yaml"
        fake_repo.files[long_path] = fake_repo.files.pop(f"packages/{name}/package.yaml")
        index = fake_repo._index()
        index["packages"][0]["path"] = long_path
        fake_repo.files["index.json"] = json.dumps(index).encode()
        repo = await _add(client, admin_user)
        r = await client.post("/api/v1/packages/install", json={"repository": repo["name"], "package": name},
                              headers=auth_headers(admin_user))
        assert r.status_code == 200, r.text
        row = (await db.execute(select(Package).where(Package.name == name))).scalar_one()
        assert row.source_repository == repo["name"] and row.source_url == RAW + long_path

    async def test_a_malformed_redirect_is_recorded_not_raised(self, client, admin_user, fake_repo):
        fake_repo.redirects[RAW + "index.json"] = "https://["
        repo = await _add(client, admin_user)
        assert "could not be fetched" in repo["last_error"] and repo["package_count"] == 0

    async def test_one_failing_repository_does_not_stop_the_scheduled_refresh(self, db, admin_user, monkeypatch):
        names = [f"r-{_uid()}", f"r-{_uid()}"]
        for name in names:
            db.add(PackageRepository(name=name, url=REPO_URL, auth={}, is_active=True, user_id=admin_user.id))
        await db.flush()
        tried = []

        @contextlib.asynccontextmanager
        async def this_session():
            yield db

        monkeypatch.setattr("app.core.database.AsyncSessionLocal", this_session)

        async def flaky(name):
            tried.append(name)
            if name == names[0]:
                raise RuntimeError("boom")

        monkeypatch.setattr(repos, "refresh_named", flaky)
        await repos.refresh_due()
        assert set(names) <= set(tried)


class TestCatalogAndInstall:
    async def test_install_from_a_repository_and_see_updates(self, client, db: AsyncSession, admin_user, fake_repo):
        name = f"demo{_uid()}"
        fake_repo.publish(name, "1.0.0")
        repo = await _add(client, admin_user)
        h = auth_headers(admin_user)

        catalog = (await client.get("/api/v1/packages/catalog", headers=h)).json()
        (entry,) = [e for e in catalog if e["name"] == name]
        assert entry["version"] == "1.0.0" and entry["installed"] == [] and not entry["update_available"]

        preview = await client.post("/api/v1/packages/preview", json={"repository": repo["name"], "package": name}, headers=h)
        assert preview.status_code == 200 and preview.json()["success"], preview.text
        installed = await client.post("/api/v1/packages/install", json={"repository": repo["name"], "package": name}, headers=h)
        assert installed.status_code == 200, installed.text
        assert installed.json()["package"]["source_repository"] == repo["name"]

        # A newer version is published: the catalog and the package say so.
        fake_repo.publish(name, "1.1.0")
        await client.post(f"/api/v1/package-repositories/{repo['name']}/refresh", headers=h)
        (entry,) = [e for e in (await client.get("/api/v1/packages/catalog", headers=h)).json() if e["name"] == name]
        assert entry["update_available"] and entry["installed"][0]["version"] == "1.0.0"
        package = (await client.get(f"/api/v1/packages/{name}", headers=h)).json()
        assert package["latest_version"] == "1.1.0" and package["update_available"]
        listed = {p["name"]: p for p in (await client.get("/api/v1/packages", headers=h)).json()}
        assert listed[name]["update_available"]

        # Updating is installing it again.
        assert (await client.post("/api/v1/packages/install", json={"repository": repo["name"], "package": name}, headers=h)).status_code == 200
        package = (await client.get(f"/api/v1/packages/{name}", headers=h)).json()
        assert package["version"] == "1.1.0" and not package["update_available"]

    async def test_a_preview_that_needs_variables_returns_their_declarations(self, client, admin_user, fake_repo):
        """A package whose YAML only validates once its variables are filled
        (a boolean field) previews as 'needs input' with the declarations,
        not a bare 400, so the console can ask for the values."""
        name = f"vars{_uid()}"
        body = (
            "apiVersion: sinas.co/v1\nkind: SinasPackage\n"
            f"package:\n  name: {name}\n  version: \"1.0.0\"\n"
            "spec:\n"
            "  variables:\n"
            "    - {name: ENABLED, type: boolean, required: true}\n"
            "    - {name: GREETING, type: text, required: true}\n"
            "  skills:\n"
            f"    - namespace: {name}\n      name: hello\n      description: Says hello\n"
            "      content: \"Say ${{ vars.GREETING }}.\"\n"
            "      isActive: ${{ vars.ENABLED }}\n"
        )
        fake_repo.publish(name, "1.0.0", body=body)
        repo = await _add(client, admin_user)
        h = auth_headers(admin_user)
        source = {"repository": repo["name"], "package": name}

        # No values yet: the placeholder isn't a boolean.
        r = await client.post("/api/v1/packages/preview", json=source, headers=h)
        assert r.status_code == 200, r.text
        data = r.json()
        assert not data["success"] and data["errors"] and data["requires_input"]
        assert [v["name"] for v in data["variables"]] == ["ENABLED", "GREETING"]

        # Some values: the missing one is reported the same way.
        r = await client.post("/api/v1/packages/preview", json={**source, "variables": {"ENABLED": True}}, headers=h)
        assert r.status_code == 200, r.text
        assert not r.json()["success"] and "GREETING" in r.json()["errors"][0] and r.json()["variables"]

        # All values: a normal preview.
        r = await client.post("/api/v1/packages/preview",
                              json={**source, "variables": {"ENABLED": True, "GREETING": "hi"}}, headers=h)
        assert r.status_code == 200 and r.json()["success"], r.text

    async def test_a_package_without_variables_still_fails_validation_plainly(self, client, admin_user):
        bad = "apiVersion: sinas.co/v1\nkind: SinasPackage\npackage:\n  name: plain\n  version: \"1\"\nspec:\n  skills:\n    - {name: x}\n"
        r = await client.post("/api/v1/packages/preview", json={"source": bad}, headers=auth_headers(admin_user))
        assert r.status_code == 400

    async def test_a_file_that_does_not_match_its_checksum_is_refused(self, client, admin_user, fake_repo):
        name = f"demo{_uid()}"
        fake_repo.publish(name, "1.0.0", tamper=True)
        repo = await _add(client, admin_user)
        r = await client.post("/api/v1/packages/install", json={"repository": repo["name"], "package": name},
                              headers=auth_headers(admin_user))
        assert r.status_code == 502 and "checksum" in r.json()["detail"]

    async def test_a_pasted_install_of_a_listed_package_follows_that_repository(self, client, admin_user, fake_repo):
        name = f"demo{_uid()}"
        fake_repo.publish(name, "2.0.0")
        await _add(client, admin_user)
        h = auth_headers(admin_user)
        r = await client.post("/api/v1/packages/install", json={"source": _package(name, "1.0.0")}, headers=h)
        assert r.status_code == 200, r.text
        package = (await client.get(f"/api/v1/packages/{name}", headers=h)).json()
        assert package["latest_version"] == "2.0.0" and package["update_available"]

    async def test_source_or_repository_not_both(self, client, admin_user):
        r = await client.post("/api/v1/packages/install", json={"source": "x", "repository": "r", "package": "p"},
                              headers=auth_headers(admin_user))
        assert r.status_code == 422


class TestConfig:
    def _config(self, *repositories, packages=None):
        return SinasConfig.model_validate({
            "apiVersion": "sinas.co/v1", "kind": "SinasConfig", "metadata": {"name": "cfg"},
            "spec": {"packageRepositories": list(repositories)},
        })

    async def test_config_declares_one_and_export_round_trips(self, db: AsyncSession, admin_user):
        name = f"r-{_uid()}"
        svc = ConfigApplyService(db, "cfg", owner_user_id=str(admin_user.id), auto_commit=False)
        result = await svc.apply_config(self._config({"name": name, "url": REPO_URL, "description": "Ours"}))
        assert result.success, result.errors
        row = (await db.execute(select(PackageRepository).where(PackageRepository.name == name))).scalar_one()
        assert row.managed_by == "config" and row.url == REPO_URL
        from app.services.resource_serializers import serialize_package_repository

        assert serialize_package_repository(row) == {"name": name, "description": "Ours", "url": REPO_URL, "isActive": True}

    async def test_config_refuses_an_unusable_url(self, db: AsyncSession, admin_user):
        svc = ConfigApplyService(db, "cfg", owner_user_id=str(admin_user.id), auto_commit=False)
        result = await svc.apply_config(self._config({"name": f"r-{_uid()}", "url": "http://plain.example.com/"}))
        assert not result.success and any("https" in e for e in result.errors), result.errors

    async def test_a_config_created_repository_is_refreshed_after_commit(self, db: AsyncSession, admin_user):
        from app.services.resources.package_repositories import PackageRepositoryRefresh

        name = f"r-{_uid()}"
        svc = ConfigApplyService(db, "cfg", owner_user_id=str(admin_user.id), auto_commit=False)
        assert (await svc.apply_config(self._config({"name": name, "url": REPO_URL}))).success
        assert PackageRepositoryRefresh(name) in svc.effects.pending

    async def test_a_package_cannot_add_a_repository(self, db: AsyncSession, admin_user):
        from app.services.package_service import PackageService

        name = f"p{_uid()}"
        yaml = (
            "apiVersion: sinas.co/v1\nkind: SinasPackage\n"
            f"package:\n  name: {name}\n  version: \"1.0.0\"\n"
            "spec:\n  packageRepositories:\n"
            f"    - name: sneaky-{name}\n      url: https://evil.example.com/\n"
        )
        await PackageService(db).install(yaml, str(admin_user.id))
        assert (await db.execute(
            select(PackageRepository).where(PackageRepository.name == f"sneaky-{name}")
        )).scalar_one_or_none() is None
