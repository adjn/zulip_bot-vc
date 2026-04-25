"""Message deletion scheduling.

State is durable: scheduled deletions live in :class:`storage.db.Storage`
so the privacy contract of auto-deleting anonymous posts survives a
bot restart.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import trio

from storage.db import Storage

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DeletionScheduler:
    """Schedules and executes message deletions.

    Durable: state lives in :class:`Storage`. The two-phase claim/delete
    pattern in :meth:`_run_once` ensures that even if the bot crashes
    between claiming a row and successfully calling ``delete_fn``, the
    row is gone -- mirroring the previous in-memory behaviour where a
    failed delete was logged-and-forgotten rather than retried forever.
    """

    # Tick interval for the scheduler loop. Override in tests.
    POLL_INTERVAL_SECONDS = 60

    def __init__(
        self,
        delete_fn: Callable[[int], Awaitable[bool]],
        storage: Storage,
        now_fn: Callable[[], datetime] = _utcnow,
    ) -> None:
        """Construct a scheduler.

        :param delete_fn: ``async fn(message_id) -> bool`` performing the
            actual delete (typically ``client.delete_message``).
        :param storage: durable backing store.
        :param now_fn: time source, overridable in tests.
        """
        self._delete_fn = delete_fn
        self._storage = storage
        self._now_fn = now_fn

    async def schedule_deletion(self, message_id: int, delete_after_minutes: float) -> None:
        """Schedule (or reschedule) deletion of a message."""
        delete_at = self._now_fn() + timedelta(minutes=delete_after_minutes)
        await self._storage.schedule_deletion(message_id, delete_at)
        logger.info(
            "Scheduled deletion message_id=%s at %s",
            message_id,
            delete_at.isoformat(),
        )

    async def run(self) -> None:
        """Main scheduler loop. Tick every ``POLL_INTERVAL_SECONDS``."""
        while True:
            try:
                await self._run_once()
            except Exception:
                logger.exception("Error in DeletionScheduler loop")
            await trio.sleep(self.POLL_INTERVAL_SECONDS)

    async def _run_once(self) -> None:
        """Claim due rows atomically, then perform deletes outside the txn.

        The claim and the API call are intentionally split:

        1. ``claim_due_deletions`` runs ``SELECT due + DELETE`` in a
           single ``BEGIN IMMEDIATE`` transaction. Once it returns, the
           rows are gone from the table -- so concurrent producers (i.e.
           ``schedule_deletion``) can't double-claim them.
        2. The Zulip API call lives outside any DB transaction. It can
           take seconds; we don't want to hold a write lock that long.

        Failure handling matches the previous in-memory version: if the
        API delete fails we log a warning and move on. Surviving
        transient failures durably is the next persistence improvement.
        """
        now = self._now_fn()
        due_ids = await self._storage.claim_due_deletions(now)

        for msg_id in due_ids:
            try:
                ok = await self._delete_fn(msg_id)
            except Exception:
                logger.exception("Delete raised for message_id=%s", msg_id)
                ok = False
            if ok:
                logger.info("Deleted message_id=%s", msg_id)
            else:
                logger.warning(
                    "Delete failed for message_id=%s; not retrying",
                    msg_id,
                )

    # --- inspection (used by tests) ------------------------------------

    async def pending_count(self) -> int:
        return await self._storage.pending_deletion_count()
