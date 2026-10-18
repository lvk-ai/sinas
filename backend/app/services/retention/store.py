"""The archive destination: PutObject to an S3-compatible bucket, nothing else.

Sinas never reads, lists, heads or deletes archive objects: the bucket
policy can (and should) grant `s3:PutObject` alone. A retry writes the same
key again instead of checking whether it exists.

boto3 is called from a thread (`asyncio.to_thread`): one PutObject per chat
in a nightly job doesn't need async I/O, and boto3 brings the AWS
credential chain (env, web identity / IRSA, ECS and EKS pod identity,
instance metadata) that "instance role" credentials need.

The restore CLI's reader is here too, but never uses these credentials: it
reads with whatever the operator provides (`open_archive_source`).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from typing import Any, Optional, Protocol

from app.core.config import settings

logger = logging.getLogger(__name__)


class ArchiveUploadError(Exception):
    """The upload failed. The message names the error code or kind, never
    the request (which holds chat content) or credentials."""


class ArchiveStore(Protocol):
    async def put(self, key: str, body: bytes, *, content_type: str, metadata: dict[str, str]) -> None:
        """Write `body` at `key`; raise ArchiveUploadError unless the
        destination confirmed the write."""


def _client_config(timeout: int):
    from botocore.config import Config

    style = settings.chat_archive_s3_addressing_style
    if style == "auto":
        # A custom endpoint (MinIO, GCS interop, R2) is reached path-style;
        # AWS S3 itself virtual-hosted.
        style = "path" if settings.chat_archive_s3_endpoint_url else "virtual"
    return Config(
        signature_version="s3v4",
        s3={"addressing_style": style},
        retries={"max_attempts": 3, "mode": "standard"},
        connect_timeout=min(timeout, 10),
        read_timeout=timeout,
        # boto3 >= 1.36 sends CRC32 checksums (aws-chunked trailers) by
        # default, which GCS's interop API and older S3-compatibles reject.
        # Content-MD5 is sent explicitly instead (below).
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    )


class S3ArchiveStore:
    def __init__(self) -> None:
        self.bucket = settings.chat_archive_s3_bucket.strip()
        self._client = None

    def _get_client(self):
        if self._client is None:
            import boto3

            if settings.chat_archive_s3_access_key_id:
                session = boto3.session.Session(
                    aws_access_key_id=settings.chat_archive_s3_access_key_id,
                    aws_secret_access_key=settings.chat_archive_s3_secret_access_key,
                    aws_session_token=settings.chat_archive_s3_session_token or None,
                )
            else:
                session = boto3.session.Session()  # the default credential chain
            self._client = session.client(
                "s3",
                endpoint_url=settings.chat_archive_s3_endpoint_url or None,
                region_name=settings.chat_archive_s3_region or None,
                config=_client_config(max(int(settings.chat_archive_s3_timeout_seconds or 60), 1)),
            )
        return self._client

    def _put_sync(self, key: str, body: bytes, content_type: str, metadata: dict[str, str]) -> None:
        from botocore.exceptions import BotoCoreError, ClientError

        kwargs: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": body,
            # Required by S3 for writes into an Object Lock bucket, and
            # integrity in transit for every provider.
            "ContentMD5": base64.b64encode(hashlib.md5(body).digest()).decode(),
            "ContentType": content_type,
            "Metadata": metadata,
        }
        if settings.chat_archive_s3_kms_key_id:
            kwargs["ServerSideEncryption"] = "aws:kms"
            kwargs["SSEKMSKeyId"] = settings.chat_archive_s3_kms_key_id
        try:
            self._get_client().put_object(**kwargs)
        except ClientError as e:
            error = e.response.get("Error", {}) if hasattr(e, "response") else {}
            status = (e.response or {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
            raise ArchiveUploadError(f"PutObject refused: {error.get('Code') or 'error'} (HTTP {status})") from None
        except BotoCoreError as e:
            raise ArchiveUploadError(f"PutObject failed: {type(e).__name__}") from None

    async def put(self, key: str, body: bytes, *, content_type: str, metadata: dict[str, str]) -> None:
        await asyncio.to_thread(self._put_sync, key, body, content_type, metadata)


def get_archive_store() -> Optional[ArchiveStore]:
    """The configured destination, or None when none is configured."""
    from app.services.retention.policy import destination_configured

    return S3ArchiveStore() if destination_configured() else None


# ---------------------------------------------------------------------------
# Restore: reading an archive back, with the operator's credentials
# ---------------------------------------------------------------------------


def parse_s3_uri(uri: str) -> tuple[str, str]:
    rest = uri[len("s3://"):]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        raise ValueError(f"'{uri}' is not an s3://bucket/key URI")
    return bucket, key


def open_archive_source(
    source: str,
    *,
    endpoint_url: Optional[str] = None,
    region: Optional[str] = None,
    profile: Optional[str] = None,
) -> bytes:
    """The bytes of an archive: a local file, or an s3:// URI read with the
    standard AWS chain (AWS_* env, --profile, an instance role): credentials
    the operator chose for reading, never Sinas's archive (write) keys."""
    if not source.startswith("s3://"):
        with open(source, "rb") as f:
            return f.read()
    import boto3
    from botocore.config import Config

    bucket, key = parse_s3_uri(source)
    session = boto3.session.Session(profile_name=profile) if profile else boto3.session.Session()
    client = session.client(
        "s3",
        endpoint_url=endpoint_url or None,
        region_name=region or None,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if endpoint_url else "virtual"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )
    return client.get_object(Bucket=bucket, Key=key)["Body"].read()
