import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict

import trio

from core.client import ZulipTrioClient

logger = logging.getLogger(__name__)


@dataclass
class ScheduledDeletion:
    message_id: int
    delete_at: datetime


class DeletionScheduler:
    """
    Very simple in-memory scheduler for deleting messages.
    No message content is stored, only message IDs and times.
    On restart, pending deletions are lost (by design for now).
    """

    def __init__(self, client: ZulipTrioClient) -> None:
        self.client = client
        self._tasks: Dict[int, ScheduledDeletion] = {}
        self._lock = trio.Lock()

    def schedule_deletion(self, message_id: int, delete_after_minutes: int) -> None:
        delete_at = datetime.now(timezone.utc) + timedelta(minutes=delete_after_minutes)
        logger.info(
            "Scheduling deletion of message_id=%s at %s",
            message_id,
            delete_at.isoformat(),
        )
        self._tasks[message_id] = ScheduledDeletion(
            message_id=message_id,
            delete_at=delete_at,
        )

    async def run(self) -> None:
        while True:
            try:
                await self._run_once()
            except Exception:  # pragma: no cover - defensive
                logger.exception("Error in DeletionScheduler loop")
            await trio.sleep(60)

    async def _run_once(self) -> None:
        now = datetime.now(timezone.utc)
        to_delete = []
        async with self._lock:
            for msg_id, sched in list(self._tasks.items()):
                if sched.delete_at <= now:
                    to_delete.append(msg_id)

        for msg_id in to_delete:
            success = await self.client.delete_message(msg_id)
            if success:
                logger.info("Deleted message_id=%s", msg_id)
            else:
                logger.warning("Failed to delete message_id=%s (see previous logs)", msg_id)
            async with self._lock:
                self._tasks.pop(msg_id, None)