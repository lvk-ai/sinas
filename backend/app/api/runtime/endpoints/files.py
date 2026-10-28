"""File runtime endpoints - upload, download, list, delete, search."""

import base64
import logging
import re
import uuid as uuid_lib
from datetime import timezone
from email.utils import format_datetime, parsedate_to_datetime
from typing import Optional

import jsonschema
from jose import JWTError, jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user_with_permissions, set_permission_used
from app.core.config import settings
from app.core.database import get_db
from app.core.permissions import check_permission
from app.models.execution import TriggerType
from app.models.file import Collection, ContentFilterEvaluation, File, FileVersion
from app.models.function import Function
from app.schemas.file import (
    CollectionResponse,
    FileDownloadResponse,
    FileMetadataUpdate,
    FileResponse,
    FileSearchMatch,
    FileSearchRequest,
    FileSearchResult,
    FileUpload,
    FileUrlResponse,
    FileVersionResponse,
    FileWithVersions,
)

from app.services.file_storage import FileStorage, get_storage, generate_file_url
from app.services.queue_service import queue_service


logger = logging.getLogger(__name__)

router = APIRouter()


def _unique_filename(name: str) -> str:
    """Append a compact unique suffix before the file extension."""
    suffix = uuid_lib.uuid4().hex[:8]
    stem, dot, ext = name.rpartition(".")
    if dot:
        return f"{stem}_{suffix}.{ext}"
    return f"{name}_{suffix}"


async def _find_name_conflict(
    db: AsyncSession,
    collection_id: uuid_lib.UUID,
    name: str,
    user_id: str,
    visibility: str,
    lock: bool = False,
) -> Optional[File]:
    """The existing file an upload named `name` collides with, if any.

    A private upload collides with the uploader's own file or a shared one
    (own first); a shared upload with a shared one.
    """
    query = select(File).where(and_(File.collection_id == collection_id, File.name == name))
    if visibility == "private":
        query = query.where(or_(File.user_id == user_id, File.visibility == "shared")).order_by(
            (File.user_id == user_id).desc()
        )
    else:
        query = query.where(File.visibility == "shared")
    query = query.limit(1)
    if lock:
        # populate_existing: the row's current_version as of the lock, not a
        # copy this session may have loaded earlier
        query = query.with_for_update().execution_options(populate_existing=True)
    return (await db.execute(query)).scalar_one_or_none()


async def _resolve_name_conflict(
    db: AsyncSession,
    existing: File,
    mode: str,
    upload_hash: str,
    namespace: str,
    collection: str,
    user_id: str,
    permissions: dict[str, bool],
    response: Response,
) -> FileResponse:
    """Answer an upload whose name is taken, in return_existing / error mode.

    Nothing is stored either way. 409 details carry the existing file's id so
    a client can tell "already there" from a real refusal.

    The existing file may be someone else's shared file, and uploading takes
    no read access. Unless the uploader could download it, the answer is a
    bare 409: no id, metadata or signed URL, and no content comparison to
    probe it with.
    """
    can_read = str(existing.user_id) == user_id or check_permission(
        permissions, f"sinas.collections/{namespace}/{collection}.download:own"
    )
    if not can_read:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "file_exists",
                "message": f"A file named '{existing.name}' already exists in this collection",
                "name": existing.name,
            },
        )

    current = (
        await db.execute(
            select(FileVersion).where(
                FileVersion.file_id == existing.id,
                FileVersion.version_number == existing.current_version,
            )
        )
    ).scalar_one_or_none()
    # The stored hash, or the hash of what was sent when a content filter
    # rewrote it on the way in
    content_matches = current is not None and upload_hash in (
        current.hash_sha256,
        current.source_hash_sha256,
    )
    conflict = {
        "file_id": str(existing.id),
        "name": existing.name,
        "current_version": existing.current_version,
        "content_matches": content_matches,
    }
    if mode == "error":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "file_exists",
                "message": f"A file named '{existing.name}' already exists in this collection",
                **conflict,
            },
        )
    if not content_matches:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "file_content_mismatch",
                "message": (
                    f"A file named '{existing.name}' already exists in this collection "
                    "with different content"
                ),
                **conflict,
            },
        )
    response.status_code = status.HTTP_200_OK
    return _file_response(existing, namespace)


def _file_response(file_record: File, namespace: str) -> FileResponse:
    return FileResponse(
        id=file_record.id,
        namespace=namespace,
        name=file_record.name,
        user_id=file_record.user_id,
        content_type=file_record.content_type,
        current_version=file_record.current_version,
        file_metadata=file_record.file_metadata,
        visibility=file_record.visibility,
        url=generate_file_url(str(file_record.id), file_record.current_version),
        created_at=file_record.created_at,
        updated_at=file_record.updated_at,
    )


def _parse_etags(header: str) -> set[str]:
    """Parse an If-None-Match header into a set of ETag values (unquoted)."""
    etags: set[str] = set()
    for part in header.split(","):
        tag = part.strip().strip('"')
        if tag:
            etags.add(tag)
    return etags


@router.get("/serve/{token}")
async def serve_file(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Serve a file via a signed JWT token (unauthenticated).

    The token contains the file_id, version, and expiry. No auth header needed.
    """
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired file token")

    if payload.get("purpose") != "file_serve":
        raise HTTPException(status_code=401, detail="Invalid token purpose")

    file_id = payload.get("file_id")
    version = payload.get("version")
    if not file_id or version is None:
        raise HTTPException(status_code=400, detail="Invalid token payload")

    # Look up file
    result = await db.execute(select(File).where(File.id == file_id))
    file_record = result.scalar_one_or_none()
    if not file_record:
        raise HTTPException(status_code=404, detail="File not found")

    # Look up version
    result = await db.execute(
        select(FileVersion).where(
            and_(
                FileVersion.file_id == file_record.id,
                FileVersion.version_number == version,
            )
        )
    )
    file_version = result.scalar_one_or_none()
    if not file_version:
        raise HTTPException(status_code=404, detail="File version not found")

    # Build caching headers
    etag = f'"{file_version.hash_sha256}"'
    last_modified_dt = file_version.created_at.replace(tzinfo=timezone.utc)
    last_modified = format_datetime(last_modified_dt, usegmt=True)
    cache_headers = {
        "Cache-Control": "private, max-age=300, immutable",
        "ETag": etag,
        "Last-Modified": last_modified,
    }

    # Conditional request: If-None-Match
    if_none_match = request.headers.get("if-none-match")
    if if_none_match:
        client_etags = _parse_etags(if_none_match)
        if file_version.hash_sha256 in client_etags:
            return Response(status_code=304, headers=cache_headers)

    # Conditional request: If-Modified-Since
    if_modified_since = request.headers.get("if-modified-since")
    if if_modified_since and not if_none_match:
        try:
            ims_dt = parsedate_to_datetime(if_modified_since)
            if last_modified_dt <= ims_dt:
                return Response(status_code=304, headers=cache_headers)
        except (ValueError, TypeError):
            pass

    # Read content
    storage: FileStorage = get_storage()
    try:
        content = await storage.read(file_version.storage_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File content not found in storage")

    # Build Content-Disposition with ASCII-safe filename + UTF-8 fallback
    from urllib.parse import quote
    ascii_name = file_record.name.encode("ascii", "replace").decode("ascii")
    content_disposition = f'inline; filename="{ascii_name}"'
    if ascii_name != file_record.name:
        content_disposition += f"; filename*=UTF-8''{quote(file_record.name)}"

    return Response(
        content=content,
        media_type=file_record.content_type,
        headers={
            "Content-Disposition": content_disposition,
            **cache_headers,
        },
    )


@router.get("/public/{namespace}/{collection}/{filename}")
async def serve_public_file(
    namespace: str,
    collection: str,
    filename: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Serve a file from a public collection (no auth required).

    Only works if the collection has is_public=True. Returns 404 otherwise
    to avoid revealing collection existence.
    """
    # Get collection and check is_public
    coll = await Collection.get_by_name(db, namespace, collection)
    if not coll or not coll.is_public:
        raise HTTPException(status_code=404, detail="Not found")

    # Get file (prefer shared for public access)
    result = await db.execute(
        select(File).where(
            and_(
                File.collection_id == coll.id,
                File.name == filename,
                File.visibility == "shared",
            )
        ).limit(1)
    )
    file_record = result.scalar_one_or_none()
    if not file_record:
        raise HTTPException(status_code=404, detail="Not found")

    # Get current version
    result = await db.execute(
        select(FileVersion).where(
            and_(
                FileVersion.file_id == file_record.id,
                FileVersion.version_number == file_record.current_version,
            )
        ).limit(1)
    )
    file_version = result.scalar_one_or_none()
    if not file_version:
        raise HTTPException(status_code=404, detail="Not found")

    # Read content
    storage: FileStorage = get_storage()
    try:
        content = await storage.read(file_version.storage_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Not found")

    return Response(
        content=content,
        media_type=file_record.content_type,
        headers={
            "Content-Disposition": f'inline; filename="{file_record.name}"',
            "Cache-Control": "public, max-age=3600",
        },
    )


@router.post("/{namespace}/{collection}", response_model=FileResponse, status_code=status.HTTP_201_CREATED)
async def upload_file(
    namespace: str,
    collection: str,
    file_data: FileUpload,
    http_request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user_data: tuple = Depends(get_current_user_with_permissions),
):
    """
    Upload a file to a collection.

    If the collection doesn't exist, it will be auto-created with defaults.
    If a file with the same name exists, `on_conflict` decides what happens
    (default `rename`: the upload is stored under a unique suffixed name).
    `return_existing` makes retries idempotent: it answers 200 with the
    existing file when its content matches, without storing anything.
    """
    user_id, permissions = current_user_data
    storage: FileStorage = get_storage()
    mode = file_data.conflict_mode

    # Check upload permission
    perm = f"sinas.collections/{namespace}/{collection}.upload:own"
    if not check_permission(permissions, perm):
        set_permission_used(http_request, perm, has_perm=False)
        raise HTTPException(status_code=403, detail="Not authorized to upload files to this collection")
    set_permission_used(http_request, perm)

    # Metering leaf: one op per authorized file upload
    from app.services import metering

    await metering.record(metering.OperationKind.UPLOAD)

    # Get or create collection
    coll = await Collection.get_by_name(db, namespace, collection)
    if not coll:
        # Auto-create collection with defaults — through the applier, like
        # every other collection write, so it shows in change history.
        from app.services.resources import rest
        from app.services.resources.collections import CollectionApplier

        applier = CollectionApplier()
        result = await rest.write(
            applier,
            rest.api_context(db, user_id),
            rest.parse_spec(applier, {"namespace": namespace, "name": collection}),
            must_create=True,
        )
        coll = result.obj
        await db.refresh(coll)

    # Validate visibility setting
    if file_data.visibility == "shared" and not coll.allow_shared_files:
        raise HTTPException(status_code=400, detail="Shared files not allowed in this collection")
    if file_data.visibility == "private" and not coll.allow_private_files:
        raise HTTPException(status_code=400, detail="Private files not allowed in this collection")

    # Validate file metadata against collection schema
    if coll.metadata_schema:
        try:
            jsonschema.validate(instance=file_data.file_metadata, schema=coll.metadata_schema)
        except jsonschema.ValidationError as e:
            raise HTTPException(
                status_code=400,
                detail=f"File metadata validation failed: {e.message}"
            )

    # Decode file content
    try:
        file_content = base64.b64decode(file_data.content_base64)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid base64 content: {str(e)}")

    # Check file size
    file_size_bytes = len(file_content)
    file_size_mb = file_size_bytes / (1024 * 1024)
    if file_size_mb > coll.max_file_size_mb:
        raise HTTPException(
            status_code=413,
            detail=f"File size {file_size_mb:.2f}MB exceeds collection limit {coll.max_file_size_mb}MB"
        )

    # Hash of the bytes as sent: what a retried upload is recognised by
    upload_hash = storage.calculate_hash(file_content)

    # return_existing / error never store anything when the name is taken, so
    # answer before spending quota or running the content filter. A file that
    # appears while the filter runs is caught again at insert time below.
    if mode in ("return_existing", "error"):
        existing_file = await _find_name_conflict(db, coll.id, file_data.name, user_id, file_data.visibility)
        if existing_file:
            return await _resolve_name_conflict(
                db, existing_file, mode, upload_hash, namespace, collection, user_id, permissions, response
            )

    # Check total storage quota
    total_size_result = await db.execute(
        select(func.coalesce(func.sum(FileVersion.size_bytes), 0))
        .join(File, FileVersion.file_id == File.id)
        .where(File.collection_id == coll.id)
    )
    current_total_bytes = total_size_result.scalar()
    max_total_bytes = coll.max_total_size_gb * 1024 * 1024 * 1024
    if current_total_bytes + file_size_bytes > max_total_bytes:
        current_gb = current_total_bytes / (1024 * 1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=f"Collection storage quota exceeded ({current_gb:.2f}GB / {coll.max_total_size_gb}GB)"
        )

    file_hash = upload_hash

    # Run content filter if configured
    approved_content = file_content
    approved_metadata = file_data.file_metadata
    filter_result = None

    if coll.content_filter_function:
        filter_namespace, filter_name = coll.content_filter_function.split("/")

        # Get function
        func_record = await Function.get_by_name(db, filter_namespace, filter_name)
        if not func_record:
            raise HTTPException(
                status_code=500,
                detail=f"Content filter function '{coll.content_filter_function}' not found"
            )

        # Execute filter
        filter_input = {
            "content_base64": file_data.content_base64,
            "namespace": namespace,
            "collection": collection,
            "filename": file_data.name,
            "content_type": file_data.content_type,
            "size_bytes": len(file_content),
            "user_metadata": file_data.file_metadata,
            "user_id": user_id,
        }

        # Generate execution ID for filter
        filter_execution_id = str(uuid_lib.uuid4())

        try:
            # Release DB connection before blocking wait — prevents connection
            # starvation when the filter function calls back into the API
            await db.commit()

            filter_result = await queue_service.enqueue_and_wait(
                function_namespace=filter_namespace,
                function_name=filter_name,
                input_data=filter_input,
                execution_id=filter_execution_id,
                trigger_type=TriggerType.MANUAL.value,
                trigger_id=f"content_filter:{namespace}/{collection}",
                user_id=user_id,
                timeout=settings.content_filter_timeout_seconds,
            )

            # Validate result structure
            if not isinstance(filter_result, dict):
                raise HTTPException(
                    status_code=500,
                    detail="Content filter must return a dict with 'approved' field"
                )

            # Check if approved
            if not filter_result.get("approved", True):
                raise HTTPException(
                    status_code=400,
                    detail=filter_result.get('reason', 'No reason provided')
                )

            # Apply modifications if provided
            if filter_result.get("modified_content"):
                try:
                    approved_content = base64.b64decode(filter_result["modified_content"])
                    file_hash = storage.calculate_hash(approved_content)
                except Exception as e:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Invalid modified_content from content filter: {str(e)}"
                    )

            # Merge filter metadata with user metadata
            if filter_result.get("metadata"):
                approved_metadata = {**file_data.file_metadata, **filter_result["metadata"]}

        except HTTPException:
            raise
        except TimeoutError:
            # Fail closed. The filter job may still finish later, but its
            # result only lands in the job's Redis keys and Execution row —
            # this request is the only thing that stores the upload, and it
            # ends here.
            raise HTTPException(
                status_code=504,
                detail=(
                    f"The content filter '{coll.content_filter_function}' did not answer "
                    f"within {settings.content_filter_timeout_seconds}s; the file was not stored"
                ),
            )
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Content filter execution failed: {str(e)}"
            )

    # Claim the name, or a version of the existing file. The partial unique
    # indexes on files (collection, name[, user]) are the arbiter between
    # concurrent uploads: the loser's insert fails, and a second pass sees
    # the winner's committed row and resolves against it like any conflict.
    for _attempt in range(2):
        existing_file = await _find_name_conflict(
            db, coll.id, file_data.name, user_id, file_data.visibility,
            # Serialize version bumps on the same file
            lock=mode == "new_version",
        )
        if existing_file and mode in ("return_existing", "error"):
            return await _resolve_name_conflict(
                db, existing_file, mode, upload_hash, namespace, collection, user_id, permissions, response
            )
        if existing_file and mode == "new_version":
            # Same rule as editing a file's metadata: your own, or upload:all
            if str(existing_file.user_id) != user_id and not check_permission(
                permissions, f"sinas.collections/{namespace}/{collection}.upload:all"
            ):
                raise HTTPException(
                    status_code=403,
                    detail=f"Not authorized to add a version to '{existing_file.name}'",
                )

        savepoint = await db.begin_nested()
        if existing_file and mode == "new_version":
            file_record = existing_file
            file_record.current_version += 1
            file_record.content_type = file_data.content_type
            file_record.file_metadata = approved_metadata
        else:
            # rename: a taken name gets a unique suffix
            final_name = _unique_filename(file_data.name) if existing_file else file_data.name
            file_record = File(
                collection_id=coll.id,
                name=final_name,
                user_id=user_id,
                content_type=file_data.content_type,
                current_version=1,
                file_metadata=approved_metadata,
                visibility=file_data.visibility,
            )
            db.add(file_record)

        try:
            await db.flush()
        except IntegrityError:
            # A concurrent upload took the name between the lookup and here
            await savepoint.rollback()
            continue
        await savepoint.commit()
        break
    else:
        raise HTTPException(
            status_code=409,
            detail=f"Could not create file '{file_data.name}' — concurrent conflict",
        )

    # Determine storage path (always unique per version)
    storage_path = f"{namespace}/{collection}/{file_record.id}/v{file_record.current_version}"

    # Save to storage FIRST, then commit DB (prevents orphan DB records)
    try:
        await storage.save(storage_path, approved_content)
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to save file to storage: {str(e)}"
        )

    # Create version record
    version = FileVersion(
        file_id=file_record.id,
        version_number=file_record.current_version,
        storage_path=storage_path,
        size_bytes=len(approved_content),
        hash_sha256=file_hash,
        source_hash_sha256=upload_hash,
        uploaded_by=user_id,
    )
    db.add(version)

    # Save content filter evaluation if filter was run
    if coll.content_filter_function and filter_result:
        evaluation = ContentFilterEvaluation(
            file_id=file_record.id,
            version_number=file_record.current_version,
            function_namespace=filter_namespace,
            function_name=filter_name,
            result=filter_result,
        )
        db.add(evaluation)

    try:
        await db.commit()
        await db.refresh(file_record)
    except Exception as e:
        # Clean up storage if DB commit fails
        try:
            await storage.delete(storage_path)
        except Exception:
            logger.warning(f"Failed to clean up storage at {storage_path} after DB error")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to save file record: {str(e)}"
        )

    # Trigger post-upload function if configured (async, don't block)
    if coll.post_upload_function:
        post_namespace, post_name = coll.post_upload_function.split("/")

        # Get post-upload function
        post_func = await Function.get_by_name(db, post_namespace, post_name)

        if post_func:
            post_input = {
                "file_id": str(file_record.id),
                "namespace": namespace,
                "collection": collection,
                "filename": file_data.name,
                "version": file_record.current_version,
                "file_path": storage_path,
                "user_id": user_id,
                "metadata": approved_metadata,
            }

            post_execution_id = str(uuid_lib.uuid4())

            try:

                await queue_service.enqueue_function(
                    function_namespace=post_namespace,
                    function_name=post_name,
                    input_data=post_input,
                    execution_id=post_execution_id,
                    trigger_type=TriggerType.MANUAL.value,
                    trigger_id=f"post_upload:{namespace}/{collection}",
                    user_id=user_id,
                )
            except Exception:
                # Don't fail upload if post-upload trigger fails
                pass

    return _file_response(file_record, namespace)


@router.get("/{namespace}/{collection}/{filename}", response_model=FileDownloadResponse)
async def download_file(
    namespace: str,
    collection: str,
    filename: str,
    version: Optional[int] = None,
    http_request: Request = None,
    db: AsyncSession = Depends(get_db),
    current_user_data: tuple = Depends(get_current_user_with_permissions),
):
    """Download a file from a collection."""
    user_id, permissions = current_user_data
    storage: FileStorage = get_storage()

    # Check download permission
    perm = f"sinas.collections/{namespace}/{collection}.download:own"
    if not check_permission(permissions, perm):
        set_permission_used(http_request, perm, has_perm=False)
        raise HTTPException(status_code=403, detail="Not authorized to download files from this collection")
    set_permission_used(http_request, perm)

    # Get collection
    coll = await Collection.get_by_name(db, namespace, collection)
    if not coll:
        raise HTTPException(status_code=404, detail="Collection not found")

    # Get file — prefer user's own, then shared
    has_all_perm = check_permission(permissions, f"sinas.collections/{namespace}/{collection}.download:all")
    file_query = select(File).where(
        and_(
            File.collection_id == coll.id,
            File.name == filename,
        )
    )
    if not has_all_perm:
        file_query = file_query.where(
            or_(File.user_id == user_id, File.visibility == "shared")
        )
    file_query = file_query.order_by(
        (File.user_id == user_id).desc()
    ).limit(1)

    result = await db.execute(file_query)
    file_record = result.scalar_one_or_none()

    if not file_record:
        raise HTTPException(status_code=404, detail="File not found")

    if file_record.visibility == "private" and str(file_record.user_id) != user_id and not has_all_perm:
        raise HTTPException(status_code=403, detail="Not authorized to access this private file")

    # Get version
    version_number = version or file_record.current_version
    result = await db.execute(
        select(FileVersion).where(
            and_(
                FileVersion.file_id == file_record.id,
                FileVersion.version_number == version_number
            )
        ).limit(1)
    )
    file_version = result.scalar_one_or_none()

    if not file_version:
        raise HTTPException(status_code=404, detail=f"Version {version_number} not found")

    # Read file content
    try:
        file_content = await storage.read(file_version.storage_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File content not found in storage")

    return FileDownloadResponse(
        content_base64=base64.b64encode(file_content).decode("utf-8"),
        content_type=file_record.content_type,
        file_metadata=file_record.file_metadata,
        version=version_number,
    )


@router.post("/{namespace}/{collection}/{filename}/url")
async def generate_temp_url(
    namespace: str,
    collection: str,
    filename: str,
    version: Optional[int] = None,
    expires_in: int = 3600,
    http_request: Request = None,
    db: AsyncSession = Depends(get_db),
    current_user_data: tuple = Depends(get_current_user_with_permissions),
):
    """
    Generate a temporary public URL for a file.

    The URL is signed with a JWT and does not require authentication to access.
    Returns a data URL (base64) if DOMAIN is not configured (localhost dev).
    """
    user_id, permissions = current_user_data

    # Reuse download permission
    perm = f"sinas.collections/{namespace}/{collection}.download:own"
    if not check_permission(permissions, perm):
        set_permission_used(http_request, perm, has_perm=False)
        raise HTTPException(status_code=403, detail="Not authorized to access files in this collection")
    set_permission_used(http_request, perm)

    # Clamp expires_in to reasonable bounds
    expires_in = max(60, min(expires_in, 2592000))  # 1 min to 30 days

    # Get collection
    coll = await Collection.get_by_name(db, namespace, collection)
    if not coll:
        raise HTTPException(status_code=404, detail="Collection not found")

    # Get file — prefer user's own, then shared
    has_all_perm = check_permission(permissions, f"sinas.collections/{namespace}/{collection}.download:all")
    file_query = select(File).where(
        and_(
            File.collection_id == coll.id,
            File.name == filename,
        )
    )
    if not has_all_perm:
        file_query = file_query.where(
            or_(File.user_id == user_id, File.visibility == "shared")
        )
    file_query = file_query.order_by(
        (File.user_id == user_id).desc()
    ).limit(1)

    result = await db.execute(file_query)
    file_record = result.scalar_one_or_none()
    if not file_record:
        raise HTTPException(status_code=404, detail="File not found")

    if file_record.visibility == "private" and str(file_record.user_id) != user_id and not has_all_perm:
        raise HTTPException(status_code=403, detail="Not authorized to access this private file")

    # Determine version
    version_number = version or file_record.current_version

    # Verify version exists
    ver_result = await db.execute(
        select(FileVersion).where(
            and_(
                FileVersion.file_id == file_record.id,
                FileVersion.version_number == version_number,
            )
        ).limit(1)
    )
    file_version = ver_result.scalar_one_or_none()
    if not file_version:
        raise HTTPException(status_code=404, detail=f"Version {version_number} not found")

    url = generate_file_url(str(file_record.id), version_number, expires_in=expires_in)

    return FileUrlResponse(
        url=url,
        filename=file_record.name,
        content_type=file_record.content_type,
        version=version_number,
        expires_in=expires_in,
    )


@router.get("/{namespace}/{collection}", response_model=list[FileWithVersions])
async def list_files(
    namespace: str,
    collection: str,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
    current_user_data: tuple = Depends(get_current_user_with_permissions),
):
    """List all files in a collection."""
    user_id, permissions = current_user_data

    # Check list permission
    perm = f"sinas.collections/{namespace}/{collection}.list:own"
    if not check_permission(permissions, perm):
        set_permission_used(http_request, perm, has_perm=False)
        raise HTTPException(status_code=403, detail="Not authorized to list files in this collection")
    set_permission_used(http_request, perm)

    # Get collection
    coll = await Collection.get_by_name(db, namespace, collection)
    if not coll:
        raise HTTPException(status_code=404, detail="Collection not found")

    # Get files
    has_all_perm = check_permission(permissions, f"sinas.collections/{namespace}/{collection}.list:all")

    query = select(File).where(File.collection_id == coll.id)

    if not has_all_perm:
        # Own files (any visibility) + non-private files from others
        query = query.where(
            or_(
                File.user_id == user_id,
                File.visibility != "private"
            )
        )

    query = query.order_by(File.name)

    result = await db.execute(query)
    files = result.scalars().all()

    # Load versions for each file
    responses = []
    for file_record in files:
        result = await db.execute(
            select(FileVersion)
            .where(FileVersion.file_id == file_record.id)
            .order_by(FileVersion.version_number.desc())
        )
        versions = result.scalars().all()

        responses.append(FileWithVersions(
            id=file_record.id,
            namespace=namespace,
            name=file_record.name,
            user_id=file_record.user_id,
            content_type=file_record.content_type,
            current_version=file_record.current_version,
            file_metadata=file_record.file_metadata,
            visibility=file_record.visibility,
            created_at=file_record.created_at,
            updated_at=file_record.updated_at,
            versions=[FileVersionResponse.model_validate(v) for v in versions]
        ))

    return responses


@router.post("/{namespace}/{collection}/search", response_model=list[FileSearchResult])
async def search_files(
    namespace: str,
    collection: str,
    search_request: FileSearchRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
    current_user_data: tuple = Depends(get_current_user_with_permissions),
):
    """Search files in a collection by metadata and/or content."""
    user_id, permissions = current_user_data
    storage: FileStorage = get_storage()

    # Reuse list permission for search
    perm = f"sinas.collections/{namespace}/{collection}.list:own"
    if not check_permission(permissions, perm):
        set_permission_used(http_request, perm, has_perm=False)
        raise HTTPException(status_code=403, detail="Not authorized to search files in this collection")
    set_permission_used(http_request, perm)

    # Get collection
    coll = await Collection.get_by_name(db, namespace, collection)
    if not coll:
        raise HTTPException(status_code=404, detail="Collection not found")

    # Build base query with visibility rules
    has_all_perm = check_permission(permissions, f"sinas.collections/{namespace}/{collection}.list:all")
    query = select(File).where(File.collection_id == coll.id)

    if not has_all_perm:
        query = query.where(
            or_(
                File.user_id == user_id,
                File.visibility == "shared"
            )
        )

    # Apply metadata filters using PostgreSQL JSON containment
    if search_request.metadata_filter:
        for key, value in search_request.metadata_filter.items():
            query = query.where(File.file_metadata[key].as_string() == str(value))

    query = query.order_by(File.name).limit(search_request.limit)

    result = await db.execute(query)
    files = result.scalars().all()

    results = []

    # If no text query, return files matching metadata filter
    if not search_request.query:
        for file_record in files:
            results.append(FileSearchResult(
                file_id=file_record.id,
                filename=file_record.name,
                version=file_record.current_version,
                matches=[],
            ))
        return results

    # Compile regex pattern
    try:
        pattern = re.compile(search_request.query)
    except re.error as e:
        raise HTTPException(status_code=400, detail=f"Invalid regex pattern: {str(e)}")

    # Known binary content types to skip
    BINARY_PREFIXES = ("image/", "audio/", "video/", "font/")
    BINARY_TYPES = {
        "application/pdf", "application/zip", "application/gzip",
        "application/x-tar", "application/x-bzip2", "application/x-7z-compressed",
        "application/vnd.openxmlformats", "application/msword",
        "application/vnd.ms-excel", "application/vnd.ms-powerpoint",
    }

    # Search file content
    for file_record in files:
        # Skip known binary types
        ct = file_record.content_type
        if any(ct.startswith(p) for p in BINARY_PREFIXES) or ct in BINARY_TYPES:
            continue

        # Get current version
        ver_result = await db.execute(
            select(FileVersion).where(
                and_(
                    FileVersion.file_id == file_record.id,
                    FileVersion.version_number == file_record.current_version
                )
            )
        )
        file_version = ver_result.scalar_one_or_none()
        if not file_version:
            continue

        # Read file content — try to decode as text, skip if binary
        try:
            content = await storage.read(file_version.storage_path)
            text = content.decode("utf-8")
        except (Exception, UnicodeDecodeError):
            continue

        lines = text.split("\n")
        matches = []

        for i, line in enumerate(lines):
            if pattern.search(line):
                # Build context (2 lines before and after)
                context_start = max(0, i - 2)
                context_end = min(len(lines), i + 3)
                context = lines[context_start:context_end]

                matches.append(FileSearchMatch(
                    line=i + 1,
                    text=line,
                    context=context,
                ))

        if matches:
            results.append(FileSearchResult(
                file_id=file_record.id,
                filename=file_record.name,
                version=file_record.current_version,
                matches=matches,
            ))

    return results


@router.patch("/{namespace}/{collection}/{filename}", response_model=FileResponse)
async def update_file_metadata(
    namespace: str,
    collection: str,
    filename: str,
    update_data: FileMetadataUpdate,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
    current_user_data: tuple = Depends(get_current_user_with_permissions),
):
    """Update file metadata."""
    user_id, permissions = current_user_data

    # Reuse upload permission for metadata edits
    perm = f"sinas.collections/{namespace}/{collection}.upload:own"
    if not check_permission(permissions, perm):
        set_permission_used(http_request, perm, has_perm=False)
        raise HTTPException(status_code=403, detail="Not authorized to update files in this collection")
    set_permission_used(http_request, perm)

    # Get collection
    coll = await Collection.get_by_name(db, namespace, collection)
    if not coll:
        raise HTTPException(status_code=404, detail="Collection not found")

    # Get file — prefer user's own, then shared
    has_all_perm = check_permission(permissions, f"sinas.collections/{namespace}/{collection}.upload:all")
    file_query = select(File).where(
        and_(
            File.collection_id == coll.id,
            File.name == filename,
        )
    )
    if not has_all_perm:
        file_query = file_query.where(
            or_(File.user_id == user_id, File.visibility == "shared")
        )
    file_query = file_query.order_by(
        (File.user_id == user_id).desc()
    ).limit(1)

    result = await db.execute(file_query)
    file_record = result.scalar_one_or_none()

    if not file_record:
        raise HTTPException(status_code=404, detail="File not found")

    if str(file_record.user_id) != user_id and not has_all_perm:
        raise HTTPException(status_code=403, detail="Not authorized to update this file")

    # Validate metadata against collection schema
    if coll.metadata_schema:
        try:
            jsonschema.validate(instance=update_data.file_metadata, schema=coll.metadata_schema)
        except jsonschema.ValidationError as e:
            raise HTTPException(
                status_code=400,
                detail=f"File metadata validation failed: {e.message}"
            )

    file_record.file_metadata = update_data.file_metadata
    await db.flush()
    await db.refresh(file_record)

    url = generate_file_url(str(file_record.id), file_record.current_version)

    return FileResponse(
        id=file_record.id,
        namespace=namespace,
        name=file_record.name,
        user_id=file_record.user_id,
        content_type=file_record.content_type,
        current_version=file_record.current_version,
        file_metadata=file_record.file_metadata,
        visibility=file_record.visibility,
        url=url,
        created_at=file_record.created_at,
        updated_at=file_record.updated_at,
    )


@router.delete("/{namespace}/{collection}/{filename}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(
    namespace: str,
    collection: str,
    filename: str,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
    current_user_data: tuple = Depends(get_current_user_with_permissions),
):
    """Delete a file and all its versions."""
    user_id, permissions = current_user_data
    storage: FileStorage = get_storage()

    # Check delete permission
    perm = f"sinas.collections/{namespace}/{collection}.delete_files:own"
    if not check_permission(permissions, perm):
        set_permission_used(http_request, perm, has_perm=False)
        raise HTTPException(status_code=403, detail="Not authorized to delete files from this collection")
    set_permission_used(http_request, perm)

    # Get collection
    coll = await Collection.get_by_name(db, namespace, collection)
    if not coll:
        raise HTTPException(status_code=404, detail="Collection not found")

    # Get file — prefer user's own, then shared
    has_all_perm = check_permission(permissions, f"sinas.collections/{namespace}/{collection}.delete_files:all")
    file_query = select(File).where(
        and_(
            File.collection_id == coll.id,
            File.name == filename,
        )
    )
    if not has_all_perm:
        file_query = file_query.where(
            or_(File.user_id == user_id, File.visibility == "shared")
        )
    file_query = file_query.order_by(
        (File.user_id == user_id).desc()
    ).limit(1)

    result = await db.execute(file_query)
    file_record = result.scalar_one_or_none()

    if not file_record:
        raise HTTPException(status_code=404, detail="File not found")

    if file_record.visibility == "private" and str(file_record.user_id) != user_id and not has_all_perm:
        raise HTTPException(status_code=403, detail="Not authorized to delete this private file")

    # Get all versions of this file
    result = await db.execute(
        select(FileVersion).where(FileVersion.file_id == file_record.id)
    )
    versions = result.scalars().all()

    # Delete physical files
    for version in versions:
        try:
            await storage.delete(version.storage_path)
        except Exception:
            # Continue even if storage deletion fails
            pass

    # Delete database record (cascade will delete versions and evaluations)
    await db.delete(file_record)
    await db.flush()

    return None
