"""Uploading to a name that is taken: the on_conflict modes (#144).

The default stays `rename` (a unique suffix), so existing clients see no
change. `return_existing` makes retries idempotent: an identical re-upload
answers 200 with the existing file and stores nothing, a different one 409s.
`error` 409s with the existing file's id, `new_version` adds a version.

The race tests use real connections and real commits (the `db` fixture's
single rolled-back transaction can't show two transactions contending for
the files' unique indexes), like test_setup_link_races.
"""
import asyncio
import base64
import os
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api.runtime.endpoints import files as files_endpoint
from app.core.auth import get_current_user_with_permissions
from app.core.database import get_db
from app.models.file import Collection, File, FileVersion
from app.models.function import Function
from app.models.user import RolePermission, User, UserRole
from app.services.queue_service import queue_service

from tests.conftest import _test_db_url, auth_headers


@pytest.fixture(autouse=True)
def _tmp_file_storage(tmp_path, monkeypatch):
    """Point file storage at a per-test temp dir (and drop the cached backend)."""
    import app.services.file_storage as fs

    monkeypatch.setenv("FILE_STORAGE_PATH", str(tmp_path / "files"))
    fs._storage = None
    yield
    fs._storage = None


def _stored_blobs(tmp_path) -> list[str]:
    root = tmp_path / "files"
    if not root.exists():
        return []
    return [os.path.join(d, f) for d, _, names in os.walk(root) for f in names]


async def _grant(db: AsyncSession, user: User, *perm_keys: str) -> None:
    role_id = (
        await db.execute(select(UserRole.role_id).where(UserRole.user_id == user.id).limit(1))
    ).scalar_one()
    for key in perm_keys:
        db.add(RolePermission(role_id=role_id, permission_key=key, permission_value=True))
    await db.flush()


@pytest_asyncio.fixture
async def coll(db: AsyncSession, test_user: User) -> Collection:
    name = f"uploads-{uuid.uuid4().hex[:8]}"
    c = Collection(namespace="default", name=name, user_id=test_user.id)
    db.add(c)
    await db.flush()
    await _grant(db, test_user, f"sinas.collections/default/{name}.upload:own")
    return c


def _body(content: bytes, name: str = "report.txt", **extra) -> dict:
    return {
        "name": name,
        "content_base64": base64.b64encode(content).decode(),
        "content_type": "text/plain",
        "visibility": "private",
        **extra,
    }


async def _upload(client, coll: Collection, user: User, content: bytes, **extra):
    return await client.post(
        f"/files/{coll.namespace}/{coll.name}",
        json=_body(content, **extra),
        headers=auth_headers(user),
    )


async def _files(db: AsyncSession, coll: Collection) -> list[File]:
    return list(
        (await db.execute(select(File).where(File.collection_id == coll.id).order_by(File.created_at)))
        .scalars()
        .all()
    )


async def _version_count(db: AsyncSession, coll: Collection) -> int:
    return (
        await db.execute(
            select(func.count()).select_from(FileVersion).join(File).where(File.collection_id == coll.id)
        )
    ).scalar_one()


class TestDefaultRename:
    """Unchanged: a taken name gets a unique suffix, for clients that send
    no on_conflict at all and for those that ask for it."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("extra", [{}, {"on_conflict": "rename"}])
    async def test_taken_name_is_renamed(self, db, client, test_user, coll, extra):
        first = await _upload(client, coll, test_user, b"one", **extra)
        second = await _upload(client, coll, test_user, b"one", **extra)

        assert first.status_code == 201, first.text
        assert second.status_code == 201, second.text
        assert first.json()["name"] == "report.txt"
        renamed = second.json()["name"]
        assert renamed != "report.txt"
        assert renamed.startswith("report_") and renamed.endswith(".txt")
        assert second.json()["id"] != first.json()["id"]
        assert [f.name for f in await _files(db, coll)] == ["report.txt", renamed]


class TestReturnExisting:
    @pytest.mark.asyncio
    async def test_free_name_is_created(self, db, client, test_user, coll):
        resp = await _upload(client, coll, test_user, b"payload", on_conflict="return_existing")

        assert resp.status_code == 201, resp.text
        assert resp.json()["name"] == "report.txt"
        assert resp.json()["current_version"] == 1

    @pytest.mark.asyncio
    async def test_identical_retry_returns_the_existing_file(
        self, db, client, test_user, coll, tmp_path
    ):
        first = await _upload(client, coll, test_user, b"payload", on_conflict="return_existing")
        blobs = _stored_blobs(tmp_path)

        retry = await _upload(client, coll, test_user, b"payload", on_conflict="return_existing")

        assert retry.status_code == 200, retry.text
        assert retry.json()["id"] == first.json()["id"]
        assert retry.json()["name"] == "report.txt"
        assert retry.json()["current_version"] == 1
        # Nothing new: one file, one version, no extra bytes
        assert len(await _files(db, coll)) == 1
        assert await _version_count(db, coll) == 1
        assert _stored_blobs(tmp_path) == blobs

    @pytest.mark.asyncio
    async def test_different_content_is_a_conflict(self, db, client, test_user, coll):
        first = await _upload(client, coll, test_user, b"payload", on_conflict="return_existing")

        resp = await _upload(client, coll, test_user, b"other payload", on_conflict="return_existing")

        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == "file_content_mismatch"
        assert detail["file_id"] == first.json()["id"]
        assert detail["name"] == "report.txt"
        assert detail["current_version"] == 1
        assert detail["content_matches"] is False
        assert len(await _files(db, coll)) == 1
        assert await _version_count(db, coll) == 1

    @pytest.mark.asyncio
    async def test_compares_against_the_current_version(self, db, client, test_user, coll):
        await _upload(client, coll, test_user, b"v1")
        await _upload(client, coll, test_user, b"v2", on_conflict="new_version")

        stale = await _upload(client, coll, test_user, b"v1", on_conflict="return_existing")
        current = await _upload(client, coll, test_user, b"v2", on_conflict="return_existing")

        assert stale.status_code == 409
        assert current.status_code == 200
        assert current.json()["current_version"] == 2

    @pytest.mark.asyncio
    async def test_metadata_is_not_compared(self, db, client, test_user, coll):
        first = await _upload(
            client, coll, test_user, b"payload", on_conflict="return_existing", file_metadata={"a": 1}
        )
        retry = await _upload(
            client, coll, test_user, b"payload", on_conflict="return_existing", file_metadata={"a": 2}
        )

        assert retry.status_code == 200
        assert retry.json()["id"] == first.json()["id"]
        assert retry.json()["file_metadata"] == {"a": 1}

    @pytest.mark.asyncio
    async def test_private_upload_matches_a_shared_file(self, db, client, test_user, coll):
        """The same files the rename check treats as taken: own, or shared."""
        shared = await _upload(client, coll, test_user, b"payload", visibility="shared")
        retry = await _upload(client, coll, test_user, b"payload", on_conflict="return_existing")

        assert retry.status_code == 200, retry.text
        assert retry.json()["id"] == shared.json()["id"]
        assert retry.json()["visibility"] == "shared"


class TestError:
    @pytest.mark.asyncio
    async def test_taken_name_409s_with_the_existing_file(self, db, client, test_user, coll, tmp_path):
        first = await _upload(client, coll, test_user, b"payload")
        blobs = _stored_blobs(tmp_path)

        same = await _upload(client, coll, test_user, b"payload", on_conflict="error")
        different = await _upload(client, coll, test_user, b"other", on_conflict="error")

        for resp, matches in ((same, True), (different, False)):
            assert resp.status_code == 409, resp.text
            detail = resp.json()["detail"]
            assert detail["code"] == "file_exists"
            assert detail["file_id"] == first.json()["id"]
            assert detail["name"] == "report.txt"
            assert detail["content_matches"] is matches
        assert len(await _files(db, coll)) == 1
        assert _stored_blobs(tmp_path) == blobs

    @pytest.mark.asyncio
    async def test_free_name_is_created(self, db, client, test_user, coll):
        resp = await _upload(client, coll, test_user, b"payload", on_conflict="error")
        assert resp.status_code == 201, resp.text


class TestNewVersion:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("extra", [{"on_conflict": "new_version"}, {"update_existing": True}])
    async def test_adds_a_version_to_the_existing_file(self, db, client, test_user, coll, extra):
        first = await _upload(client, coll, test_user, b"v1")

        resp = await _upload(client, coll, test_user, b"v2", file_metadata={"rev": 2}, **extra)

        assert resp.status_code == 201, resp.text
        assert resp.json()["id"] == first.json()["id"]
        assert resp.json()["name"] == "report.txt"
        assert resp.json()["current_version"] == 2
        assert resp.json()["file_metadata"] == {"rev": 2}
        assert len(await _files(db, coll)) == 1
        assert await _version_count(db, coll) == 2

    @pytest.mark.asyncio
    async def test_free_name_is_created(self, db, client, test_user, coll):
        resp = await _upload(client, coll, test_user, b"v1", on_conflict="new_version")
        assert resp.status_code == 201, resp.text
        assert resp.json()["current_version"] == 1

    @pytest.mark.asyncio
    async def test_someone_elses_shared_file_needs_upload_all(
        self, db, client, test_user, admin_user, coll
    ):
        """Like editing its metadata: adding a version to a file you don't own
        takes upload:all."""
        theirs = await _upload(client, coll, admin_user, b"theirs", visibility="shared")
        assert theirs.status_code == 201, theirs.text

        resp = await _upload(client, coll, test_user, b"mine", on_conflict="new_version")

        assert resp.status_code == 403, resp.text
        assert await _version_count(db, coll) == 1

        await _grant(db, test_user, f"sinas.collections/default/{coll.name}.upload:all")
        resp = await _upload(client, coll, test_user, b"mine", on_conflict="new_version")
        assert resp.status_code == 201, resp.text
        assert resp.json()["id"] == theirs.json()["id"]
        assert resp.json()["current_version"] == 2

    @pytest.mark.asyncio
    async def test_update_existing_cannot_contradict_on_conflict(self, db, client, test_user, coll):
        resp = await _upload(client, coll, test_user, b"v1", update_existing=True, on_conflict="error")
        assert resp.status_code == 422, resp.text

    @pytest.mark.asyncio
    async def test_unknown_mode_is_rejected(self, db, client, test_user, coll):
        resp = await _upload(client, coll, test_user, b"v1", on_conflict="overwrite")
        assert resp.status_code == 422, resp.text


class TestContentFilter:
    """The filter still runs wherever bytes are written, and a file it
    rewrote is still recognised when its upload is retried."""

    @pytest.fixture
    def redacting_filter(self, monkeypatch):
        calls = []

        async def _get_by_name(cls, db, namespace, name):
            return object()

        async def _enqueue_and_wait(**kwargs):
            calls.append(kwargs)
            return {"approved": True, "modified_content": base64.b64encode(b"[redacted]").decode()}

        monkeypatch.setattr(Function, "get_by_name", classmethod(_get_by_name))
        monkeypatch.setattr(queue_service, "enqueue_and_wait", _enqueue_and_wait)
        return calls

    @pytest_asyncio.fixture
    async def filtered_coll(self, db, coll):
        coll.content_filter_function = "filters/redact"
        await db.flush()
        return coll

    @pytest.mark.asyncio
    async def test_retry_of_a_rewritten_file_matches(
        self, db, client, test_user, filtered_coll, redacting_filter
    ):
        first = await _upload(client, filtered_coll, test_user, b"secret", on_conflict="return_existing")
        assert first.status_code == 201, first.text
        assert len(redacting_filter) == 1

        retry = await _upload(client, filtered_coll, test_user, b"secret", on_conflict="return_existing")

        assert retry.status_code == 200, retry.text
        assert retry.json()["id"] == first.json()["id"]
        # Nothing is written, so the filter has nothing to check
        assert len(redacting_filter) == 1
        version = (
            await db.execute(select(FileVersion).where(FileVersion.file_id == uuid.UUID(first.json()["id"])))
        ).scalar_one()
        assert version.hash_sha256 != version.source_hash_sha256

    @pytest.mark.asyncio
    async def test_filter_runs_for_every_written_mode(
        self, db, client, test_user, filtered_coll, redacting_filter
    ):
        await _upload(client, filtered_coll, test_user, b"a")
        await _upload(client, filtered_coll, test_user, b"a")  # rename
        await _upload(client, filtered_coll, test_user, b"b", on_conflict="new_version")
        assert len(redacting_filter) == 3

        resp = await _upload(client, filtered_coll, test_user, b"c", on_conflict="error")
        assert resp.status_code == 409
        assert len(redacting_filter) == 3


# ------------------------------------------------------------------ races


@pytest_asyncio.fixture
async def race_env():
    """A committed user and collection, and an app whose requests each get
    their own real session (committed by get_db's normal path)."""
    from app.main import app as _app
    from app.main import management_app

    engine = create_async_engine(_test_db_url)
    name = f"race-{uuid.uuid4().hex[:8]}"
    async with AsyncSession(engine, expire_on_commit=False) as setup:
        user = User(email=f"{name}@example.com")
        setup.add(user)
        await setup.flush()
        coll = Collection(namespace="default", name=name, user_id=user.id)
        setup.add(coll)
        await setup.commit()

    async def _real_db():
        async with AsyncSession(engine, expire_on_commit=False) as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _auth():
        return str(user.id), {f"sinas.collections/default/{name}.upload:own": True}

    for a in (_app, management_app):
        a.dependency_overrides[get_db] = _real_db
        a.dependency_overrides[get_current_user_with_permissions] = _auth
    try:
        async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as client:
            yield engine, client, coll
    finally:
        for a in (_app, management_app):
            a.dependency_overrides.clear()
        async with AsyncSession(engine) as cleanup:
            await cleanup.execute(delete(Collection).where(Collection.id == coll.id))
            await cleanup.execute(delete(User).where(User.id == user.id))
            await cleanup.commit()
        await engine.dispose()


@pytest.fixture
def lookups_meet(monkeypatch):
    """Hold each request at its name lookups until the other gets there too,
    so both find the name free and both insert: the unique index, not luck,
    has to decide."""
    barrier = asyncio.Barrier(2)
    real = files_endpoint._find_name_conflict
    results = []

    async def _find(*args, **kwargs):
        found = await real(*args, **kwargs)
        results.append(found)
        if found is None:
            await asyncio.wait_for(barrier.wait(), timeout=10)
        return found

    monkeypatch.setattr(files_endpoint, "_find_name_conflict", _find)
    return results


class TestRaces:
    @pytest.mark.asyncio
    async def test_identical_concurrent_uploads_create_one_file(self, race_env, lookups_meet):
        engine, client, coll = race_env
        body = _body(b"payload", on_conflict="return_existing")

        responses = await asyncio.gather(
            client.post(f"/files/default/{coll.name}", json=body),
            client.post(f"/files/default/{coll.name}", json=body),
        )

        assert sorted(r.status_code for r in responses) == [200, 201], [r.text for r in responses]
        assert responses[0].json()["id"] == responses[1].json()["id"]
        # Both found the name free twice (pre-check, insert); the loser's
        # insert failed on the unique index and its re-lookup found the winner
        assert [found is None for found in lookups_meet] == [True, True, True, True, False]
        async with AsyncSession(engine) as s:
            assert (
                await s.execute(select(func.count()).select_from(File).where(File.collection_id == coll.id))
            ).scalar_one() == 1

    @pytest.mark.asyncio
    async def test_concurrent_new_versions_both_land(self, race_env, lookups_meet):
        engine, client, coll = race_env

        responses = await asyncio.gather(
            client.post(f"/files/default/{coll.name}", json=_body(b"a", on_conflict="new_version")),
            client.post(f"/files/default/{coll.name}", json=_body(b"b", on_conflict="new_version")),
        )

        assert [r.status_code for r in responses] == [201, 201], [r.text for r in responses]
        assert sorted(r.json()["current_version"] for r in responses) == [1, 2]
        async with AsyncSession(engine) as s:
            assert (
                await s.execute(select(func.count()).select_from(File).where(File.collection_id == coll.id))
            ).scalar_one() == 1
            assert (
                await s.execute(
                    select(func.count()).select_from(FileVersion).join(File).where(File.collection_id == coll.id)
                )
            ).scalar_one() == 2

    @pytest.mark.asyncio
    async def test_concurrent_default_uploads_still_rename(self, race_env, lookups_meet):
        engine, client, coll = race_env
        body = _body(b"payload")

        responses = await asyncio.gather(
            client.post(f"/files/default/{coll.name}", json=body),
            client.post(f"/files/default/{coll.name}", json=body),
        )

        assert [r.status_code for r in responses] == [201, 201], [r.text for r in responses]
        names = sorted(r.json()["name"] for r in responses)
        assert names[0] == "report.txt" and names[1].startswith("report_")
