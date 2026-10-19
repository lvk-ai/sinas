"""Hard-delete chats whose expires_at has passed."""

import logging
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.chat import Chat
from app.services.chat_deletion import delete_chats_and_rows, remove_stored_files

logger = logging.getLogger(__name__)

# Chats per transaction: short transactions, a bounded statement each.
BATCH_SIZE = 100


def expired_stmt(now: datetime):
    """The chats whose expires_at has passed."""
    return select(Chat.id).where(Chat.expires_at.isnot(None), Chat.expires_at < now)


async def _delete_one(chat_id) -> bool:
    """Delete one chat in its own transaction, unless another holds its
    row. True if it was deleted."""
    async with AsyncSessionLocal() as db:
        locked = (
            await db.execute(select(Chat.id).where(Chat.id == chat_id).with_for_update(skip_locked=True))
        ).scalar_one_or_none()
        if locked is None:
            return False
        _, paths = await delete_chats_and_rows(db, [chat_id])
        await db.commit()
    await remove_stored_files(paths)
    return True


async def cleanup_expired_chats() -> None:
    """Delete expired chats with everything they own, in batches of short
    transactions. A chat whose row another transaction holds (a turn
    writing to it) is skipped and goes on a later run; a chat that fails to
    delete is logged and skipped, never retried in a loop or allowed to
    block the chats after it."""
    now = datetime.now(UTC)
    total_deleted = 0
    failed: set = set()
    passed_over: set = set()  # failed, or held by another transaction: for a later run
    while True:
        async with AsyncSessionLocal() as db:
            stmt = expired_stmt(now)
            if passed_over:
                stmt = stmt.where(Chat.id.notin_(passed_over))
            ids = (
                await db.execute(
                    stmt.order_by(Chat.expires_at, Chat.id).limit(BATCH_SIZE).with_for_update(skip_locked=True)
                )
            ).scalars().all()
            if not ids:
                break
            try:
                _, paths = await delete_chats_and_rows(db, ids)
                await db.commit()
            except Exception as e:
                await db.rollback()
                logger.warning("Expired-chat cleanup: batch of %d failed (%s); retrying one by one", len(ids), e)
                paths = None
        if paths is not None:
            await remove_stored_files(paths)
            total_deleted += len(ids)
            continue
        # Isolate the chat(s) that fail so the rest still go.
        for chat_id in ids:
            try:
                if await _delete_one(chat_id):
                    total_deleted += 1
                else:
                    passed_over.add(chat_id)
            except Exception as e:
                failed.add(chat_id)
                passed_over.add(chat_id)
                logger.error("Expired-chat cleanup: could not delete chat %s: %s", chat_id, e)

    if total_deleted:
        logger.info("Cleaned up %d expired chat(s)", total_deleted)
    if failed:
        logger.error("Expired-chat cleanup: %d expired chat(s) could not be deleted", len(failed))
