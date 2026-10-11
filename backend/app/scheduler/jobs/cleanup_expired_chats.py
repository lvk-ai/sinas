"""Hard-delete chats whose expires_at has passed."""

import logging
from datetime import datetime, timezone

from sqlalchemy import delete, select

from app.core.database import AsyncSessionLocal
from app.models.chat import Chat

logger = logging.getLogger(__name__)


async def delete_workbenches_for_chats(db, chat_ids) -> list[str]:
    """Delete the workbench collections of `chat_ids` (rows only; the
    caller commits) and return the storage paths of their file versions."""
    from app.models.file import Collection, File, FileVersion

    names = [str(cid) for cid in chat_ids]
    workbench_ids = (
        await db.execute(
            select(Collection.id).where(Collection.namespace == "_chat", Collection.name.in_(names))
        )
    ).scalars().all()
    if not workbench_ids:
        return []
    paths = (
        await db.execute(
            select(FileVersion.storage_path)
            .join(File, FileVersion.file_id == File.id)
            .where(File.collection_id.in_(workbench_ids))
        )
    ).scalars().all()
    await db.execute(delete(Collection).where(Collection.id.in_(workbench_ids)))
    return sorted(set(paths))


async def remove_stored_files(paths) -> None:
    """Best effort: a stale blob is a leak, never a reason to keep a chat."""
    if not paths:
        return
    from app.services.file_storage import get_storage

    storage = get_storage()
    for path in paths:
        try:
            await storage.delete(path)
        except Exception as e:  # pragma: no cover - storage-specific
            logger.warning("Could not remove stored file %s: %s", path, e)


async def cleanup_expired_chats() -> None:
    """Delete expired chats in batches to avoid long transactions."""
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc)
        total_deleted = 0
        while True:
            result = await db.execute(
                select(Chat.id)
                .where(Chat.expires_at.isnot(None), Chat.expires_at < now)
                .limit(100)
            )
            ids = [r[0] for r in result.all()]
            if not ids:
                break
            # The chats' workbenches go with them: the collection rows (files
            # and versions cascade) now, the stored bytes after the commit.
            paths = await delete_workbenches_for_chats(db, ids)
            await db.execute(delete(Chat).where(Chat.id.in_(ids)))
            await db.commit()
            await remove_stored_files(paths)
            total_deleted += len(ids)

        if total_deleted:
            logger.info(f"Cleaned up {total_deleted} expired chat(s)")
