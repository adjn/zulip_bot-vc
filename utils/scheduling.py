"""Message deletion scheduling.

In-memory only — restart loses pending deletions. The privacy contract of
auto-deleting anonymous posts therefore depends on the bot staying up; a
durable replacement is tracked as a long-term recommendation.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import trio

logger = logging.getLogger(__name__)


@dataclass
class ScheduledDeletion:
    message_id: int
    delete_at: datetime


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DeletionScheduler:
    """Schedules and executes message deletions.

    The single trio-task assumption is documented but not relied on: every
    mutation of ``_tasks`` is performed under ``_lock``, so concurrent
    producers and the consumer cannot race on the scan-then-pop boundary.
    """

    # Tick interval for the scheduler loop. Override in tests.
    POLL_INTERVAL_SECONDS = 60

    def __init__(
        self,
        delete_fn: Callable[[int], Awaitable[bool]],
        now_fn: Callable[[], datetime] = _utcnow,
    ) -> None:
        """Construct a scheduler.

        :param delete_fn: ``async fn(message_id) -> bool`` performing the
            actual delete (typically ``client.delete_message``).
        :param now_fn: time source, overridable in tests.
        """
        self._delete_fn = delete_fn
        self._now_fn = now_fn
        self._tasks: dict[int, ScheduledDeletion] = {}
        self._lock = trio.Lock()

    async def schedule_deletion(self, message_id: int, delete_after_minutes: float) -> None:
        """Schedule (or reschedule) deletion of a message."""
        delete_at = self._now_fn() + timedelta(minutes=delete_after_minutes)
        async with self._lock:
            self._tasks[message_id] = ScheduledDeletion(message_id, delete_at)
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
        """Pop due tasks atomically, then perform deletes outside the lock."""
        # Two-phase design:
        #   1. Under the lock, scan-and-pop everything that's due. This is
        #      atomic w.r.t. concurrent `schedule_deletion()` calls, so we
        #      can't pop a message that was just rescheduled to a later time.
        #   2. Release the lock, then `await` the actual deletes. The Zulip
        #      API call can take seconds; holding the lock across it would
        #      block every producer (e.g. SEND from a user) for that long.
        now = self._now_fn()
        due: list[ScheduledDeletion] = []
        async with self._lock:
            for msg_id, sched in list(self._tasks.items()):
                if sched.delete_at <= now:
                    due.append(sched)
                    self._tasks.pop(msg_id, None)

        for sched in due:
            try:
                ok = await self._delete_fn(sched.message_id)
            except Exception:
                logger.exception("Delete raised for message_id=%s", sched.message_id)
                ok = False
            if ok:
                logger.info("Deleted message_id=%s", sched.message_id)
            else:
                # Don't re-queue indefinitely: we have no notion of permanent
                # vs transient failure today. Surface as a warning so it's
                # noticed; durable retry is part of the long-term persistence
                # work.
                logger.warning(
                    "Delete failed for message_id=%s; not retrying",
                    sched.message_id,
                )

    # --- inspection (used by tests) ------------------------------------

    def pending_count(self) -> int:
        return len(self._tasks)
