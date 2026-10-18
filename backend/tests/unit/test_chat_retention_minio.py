"""Chat retention against real S3-compatible storage (MinIO), end to end.

Skipped unless SINAS_TEST_MINIO_ENDPOINT is set. Expects a bucket created
with Object Lock (versioning on) and a default retention, and two users:

- the writer Sinas runs as, whose only permission is s3:PutObject on
  <bucket>/sinas/chats/*;
- a reader for the restore CLI, with s3:GetObject on the same prefix.

Proves that a run archives and deletes with PutObject alone (the writer is
refused Get, Head, List and Delete), that Content-MD5 satisfies the Object
Lock bucket, that a refused upload deletes nothing, and that the restore
CLI reads the archive back with the reader's credentials.

    SINAS_TEST_MINIO_ENDPOINT, SINAS_TEST_MINIO_BUCKET,
    SINAS_TEST_MINIO_WRITER_KEY / _SECRET, SINAS_TEST_MINIO_READER_KEY / _SECRET
"""

import os

import pytest

from app.core.config import settings
from app.services.retention import job
from app.services.retention.archive import parse_archive
from app.services.retention.restore import plan_restore
from app.services.retention.store import S3ArchiveStore, open_archive_source
from tests.unit.test_chat_retention_job import _retention_on, engine, world  # noqa: F401  (fixtures)

ENDPOINT = os.environ.get("SINAS_TEST_MINIO_ENDPOINT")
pytestmark = pytest.mark.skipif(not ENDPOINT, reason="no MinIO (SINAS_TEST_MINIO_ENDPOINT unset)")

BUCKET = os.environ.get("SINAS_TEST_MINIO_BUCKET", "")
WRITER = (os.environ.get("SINAS_TEST_MINIO_WRITER_KEY"), os.environ.get("SINAS_TEST_MINIO_WRITER_SECRET"))
READER = (os.environ.get("SINAS_TEST_MINIO_READER_KEY"), os.environ.get("SINAS_TEST_MINIO_READER_SECRET"))


def _client(key, secret):
    import boto3
    from botocore.config import Config

    return boto3.session.Session(aws_access_key_id=key, aws_secret_access_key=secret).client(
        "s3", endpoint_url=ENDPOINT, region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}, request_checksum_calculation="when_required",
                      response_checksum_validation="when_required", retries={"max_attempts": 1}),
    )


def _denied(call) -> bool:
    from botocore.exceptions import ClientError

    try:
        call()
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return code in ("AccessDenied", "403") or status == 403
    return False


@pytest.fixture
def as_writer(monkeypatch):
    monkeypatch.setattr(settings, "chat_archive_s3_bucket", BUCKET)
    monkeypatch.setattr(settings, "chat_archive_s3_endpoint_url", ENDPOINT)
    monkeypatch.setattr(settings, "chat_archive_s3_region", "us-east-1")
    monkeypatch.setattr(settings, "chat_archive_s3_access_key_id", WRITER[0])
    monkeypatch.setattr(settings, "chat_archive_s3_secret_access_key", WRITER[1])
    monkeypatch.setattr(settings, "chat_archive_s3_prefix", "sinas/chats")


async def test_put_only_archive_then_delete_and_restore_read(world, as_writer, monkeypatch):  # noqa: F811 (fixture)
    chat_id = await world.chat(idle_days=100, messages=3)
    result = await job.run_retention(trigger="cli", store=S3ArchiveStore(), engine=world.engine)
    assert result["status"] == "completed", result
    assert result["counts"]["archived"] == 1 and result["counts"]["deleted"] == 1, result
    assert not await world.exists(chat_id)

    # The key follows from the chat's id and creation month (World.chat
    # creates chats a day before their last update): nobody needs to list.
    from datetime import UTC, datetime, timedelta

    created = datetime.now(UTC) - timedelta(days=101)
    key = f"sinas/chats/{created:%Y}/{created:%m}/{chat_id}.jsonl.gz"

    writer = _client(*WRITER)
    assert _denied(lambda: writer.get_object(Bucket=BUCKET, Key=key)), "writer could read"
    assert _denied(lambda: writer.head_object(Bucket=BUCKET, Key=key)), "writer could head"
    assert _denied(lambda: writer.list_objects_v2(Bucket=BUCKET, Prefix="sinas/")), "writer could list"
    assert _denied(lambda: writer.delete_object(Bucket=BUCKET, Key=key)), "writer could delete"

    # The restore CLI's reader: the standard AWS chain, here from env.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", READER[0])
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", READER[1])
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    data = open_archive_source(f"s3://{BUCKET}/{key}", endpoint_url=ENDPOINT, region="us-east-1")
    archive = parse_archive(data)
    assert archive.chat["id"] == str(chat_id)
    assert len(archive.messages) == 3
    async with world.sessions() as db:
        plan = await plan_restore(db, archive)
    assert plan.ok, plan.conflicts

    # The object carries its metadata, and the bucket put it under the
    # default Object Lock retention.
    obj = _client(*READER).get_object(Bucket=BUCKET, Key=key)
    assert obj["Metadata"]["sinas-chat-id"] == str(chat_id)
    assert obj["Metadata"]["sinas-format-version"] == "1"
    root = os.environ.get("SINAS_TEST_MINIO_ROOT_KEY"), os.environ.get("SINAS_TEST_MINIO_ROOT_SECRET")
    if all(root):  # reading lock state takes s3:GetObjectRetention, which neither user has
        retention = _client(*root).get_object_retention(Bucket=BUCKET, Key=key)["Retention"]
        assert retention["Mode"] == "GOVERNANCE"


async def test_refused_upload_deletes_nothing(world, as_writer, monkeypatch):  # noqa: F811 (fixture)
    # A prefix outside the writer's policy: PutObject is refused.
    monkeypatch.setattr(settings, "chat_archive_s3_prefix", "elsewhere")
    chat_id = await world.chat(idle_days=100)
    result = await job.run_retention(trigger="cli", store=S3ArchiveStore(), engine=world.engine)
    assert result["counts"]["failed"] == {"upload": 1}, result
    assert result["counts"]["deleted"] == 0
    assert await world.exists(chat_id)


async def test_rerun_writes_the_same_key_again(world, as_writer):  # noqa: F811 (fixture)
    """Idempotent retry under Object Lock: a second PutObject to the same key
    is a new version, which a locked bucket accepts."""
    from datetime import UTC, datetime

    store = S3ArchiveStore()
    body = b"x" * 10
    key = f"sinas/chats/{datetime.now(UTC):%Y}/rerun-{os.getpid()}.jsonl.gz"
    await store.put(key, body, content_type="application/x-ndjson", metadata={"sinas-chat-id": "t"})
    await store.put(key, body, content_type="application/x-ndjson", metadata={"sinas-chat-id": "t"})
