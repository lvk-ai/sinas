"""Content filters that don't answer in time fail the upload closed.

A stuck or slow content filter used to hold the upload request for the
generic queue timeout (5 minutes) and then surface as a 500. Uploads now wait
at most content_filter_timeout_seconds and reject with a clear 504; nothing
is stored and no File row is created, on the collection upload path and the
workbench paths (upload, promote) alike.
"""
import base64
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.chat import Chat
from app.models.file import Collection, ContentFilterEvaluation, File, FileVersion
from app.models.function import Function
from app.models.user import RolePermission, User, UserRole
from app.services.queue_service import queue_service
from app.services.workbench import WorkbenchTools, get_or_create_workbench

from tests.conftest import auth_headers


@pytest.fixture(autouse=True)
def _tmp_file_storage(tmp_path, monkeypatch):
    """Point file storage at a per-test temp dir (and drop the cached backend)."""
    import app.services.file_storage as fs

    monkeypatch.setenv("FILE_STORAGE_PATH", str(tmp_path / "files"))
    fs._storage = None
    yield
    fs._storage = None


@pytest.fixture
def stuck_filter(monkeypatch):
    """The filter function exists, but its job never answers: enqueue_and_wait
    raises TimeoutError as it does when its deadline passes."""
    calls = []

    async def _get_by_name(cls, db, namespace, name):
        return object()

    async def _enqueue_and_wait(**kwargs):
        calls.append(kwargs)
        raise TimeoutError(f"Job {kwargs['execution_id']} timed out after {kwargs.get('timeout')}s")

    monkeypatch.setattr(Function, "get_by_name", classmethod(_get_by_name))
    monkeypatch.setattr(queue_service, "enqueue_and_wait", _enqueue_and_wait)
    monkeypatch.setattr(settings, "content_filter_timeout_seconds", 7)
    return calls


@pytest_asyncio.fixture
async def chat(db: AsyncSession, test_user: User) -> Chat:
    c = Chat(user_id=test_user.id, title="content filter timeout")
    db.add(c)
    await db.flush()
    await db.refresh(c)
    return c


async def _grant(db: AsyncSession, user: User, *perm_keys: str) -> None:
    role_id = (
        await db.execute(select(UserRole.role_id).where(UserRole.user_id == user.id).limit(1))
    ).scalar_one()
    for key in perm_keys:
        db.add(RolePermission(role_id=role_id, permission_key=key, permission_value=True))
    await db.flush()


def _stored_blobs(tmp_path) -> list[str]:
    root = tmp_path / "files"
    if not root.exists():
        return []
    return [os.path.join(d, f) for d, _, files in os.walk(root) for f in files]


class TestCollectionUpload:
    @pytest.mark.asyncio
    async def test_timed_out_filter_rejects_and_stores_nothing(
        self, db, client, test_user, stuck_filter, tmp_path
    ):
        name = f"filtered-{uuid.uuid4().hex[:8]}"
        coll = Collection(
            namespace="default", name=name, user_id=test_user.id,
            content_filter_function="filters/slow",
        )
        db.add(coll)
        await db.flush()
        await _grant(db, test_user, f"sinas.collections/default/{name}.upload:own")

        resp = await client.post(
            f"/files/default/{name}",
            json={
                "name": "photo.png",
                "content_base64": base64.b64encode(b"not yet scanned").decode(),
                "content_type": "image/png",
                "visibility": "private",
            },
            headers=auth_headers(test_user),
        )

        assert resp.status_code == 504, resp.text
        assert resp.json()["detail"] == (
            "The content filter 'filters/slow' did not answer within 7s; the file was not stored"
        )
        # The wait used the dedicated setting, not the generic queue timeout.
        (call,) = stuck_filter
        assert call["timeout"] == 7
        # Fail closed: no row, no version, no evaluation, no bytes.
        assert (await db.execute(
            select(func.count()).select_from(File).where(File.collection_id == coll.id)
        )).scalar_one() == 0
        assert (await db.execute(
            select(func.count()).select_from(FileVersion).join(File).where(File.collection_id == coll.id)
        )).scalar_one() == 0
        assert (await db.execute(
            select(func.count()).select_from(ContentFilterEvaluation).join(File).where(File.collection_id == coll.id)
        )).scalar_one() == 0
        assert _stored_blobs(tmp_path) == []


class TestWorkbench:
    @pytest.mark.asyncio
    async def test_timed_out_filter_rejects_workbench_upload(
        self, db, chat, client, test_user, stuck_filter, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(settings, "workbench_content_filter_function", "filters/slow")

        resp = await client.post(
            f"/chats/{chat.id}/workbench/files",
            json={"name": "notes.txt", "content_base64": base64.b64encode(b"x").decode()},
            headers=auth_headers(test_user),
        )

        assert resp.status_code == 504, resp.text
        assert resp.json()["detail"] == (
            "The content filter 'filters/slow' did not answer within 7s; the file was not stored"
        )
        (call,) = stuck_filter
        assert call["timeout"] == 7
        wb = await get_or_create_workbench(db, chat)
        assert (await db.execute(
            select(func.count()).select_from(File).where(File.collection_id == wb.id)
        )).scalar_one() == 0
        assert _stored_blobs(tmp_path) == []

    @pytest.mark.asyncio
    async def test_timed_out_filter_blocks_promote(self, db, chat, test_user, stuck_filter):
        await _grant(db, test_user, "sinas.collections/*/*.upload:own")
        target = Collection(
            namespace="ns", name=f"t-{uuid.uuid4().hex[:8]}", user_id=test_user.id,
            content_filter_function="filters/slow",
        )
        db.add(target)
        await db.flush()
        tools = WorkbenchTools()
        wrote = await tools.execute_tool(
            db, chat, str(test_user.id), "workbench_write", {"filename": "in.txt", "content": "dirty"}
        )
        assert "error" not in wrote, wrote

        out = await tools.execute_tool(
            db, chat, str(test_user.id), "workbench_promote",
            {"filename": "in.txt", "collection": f"ns/{target.name}"},
        )

        assert out["error"] == "Content filter timed out" and out["timed_out"] is True
        assert "did not answer within 7s" in out["message"]
        assert (await db.execute(
            select(func.count()).select_from(File).where(File.collection_id == target.id)
        )).scalar_one() == 0


@pytest.mark.parametrize("value", [0, -5])
def test_the_timeout_must_be_positive(value):
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError, match="CONTENT_FILTER_TIMEOUT_SECONDS"):
        Settings(content_filter_timeout_seconds=value)
