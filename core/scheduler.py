"""Generic polling scheduler.

Why this exists
---------------

Until this module landed there was a single scheduler in
:mod:`utils.scheduling` whose ``run`` loop was hard-wired to one job:
deleting expired anonymous-post messages. The next thing we want to
schedule is a delayed welcome DM for new users (see the
``welcome-feature`` todo), and after that there'll be more.

Rather than ship N stand-alone polling loops that each re-implement
"sleep, tick, swallow exceptions, repeat", this module owns the loop
once. Concrete schedulers (e.g.
:class:`utils.scheduling.DeletionScheduler`) simply expose a
``tick()`` coroutine and register it with a single
:class:`Scheduler` at startup.

Design notes
------------

* One trio task. The :class:`Scheduler` runs on a single nursery task;
  registered ticks are awaited sequentially within a single poll
  interval. We do not care about latency between two ticks today —
  at the message-deletion granularity (minutes) sequential is fine,
  and it keeps the SQLite write pattern simple.

* Failure isolation. A failing tick logs and is retried on the next
  interval, never killing the loop or sibling ticks.

* No retries inside a tick. Each registered task is responsible for
  its own retry policy (e.g. re-scheduling a row, or accepting that
  a failed action is final). The scheduler's only job is "call the
  tick on a clock".
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import trio

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScheduledTask:
    """A single registered task: a name (for logs) and a tick coroutine."""

    name: str
    tick: Callable[[], Awaitable[None]]


class Scheduler:
    """Polls registered tasks every ``poll_interval_seconds``."""

    def __init__(self, *, poll_interval_seconds: float = 60.0) -> None:
        self._tasks: list[ScheduledTask] = []
        self._poll_interval_seconds = poll_interval_seconds

    def register(self, name: str, tick: Callable[[], Awaitable[None]]) -> None:
        """Register a tick callable to be invoked every poll interval."""
        self._tasks.append(ScheduledTask(name=name, tick=tick))

    @property
    def poll_interval_seconds(self) -> float:
        return self._poll_interval_seconds

    async def run(self) -> None:
        """Main loop. Never returns under normal operation."""
        while True:
            for task in list(self._tasks):
                # Snapshot so a tick that mutates the list (e.g. dynamic
                # registration during runtime) doesn't break iteration.
                try:
                    await task.tick()
                except Exception:
                    logger.exception("scheduler task %s tick failed", task.name)
            await trio.sleep(self._poll_interval_seconds)

    async def tick_once(self) -> None:
        """Run every registered tick once. Test helper / one-shot exec."""
        for task in list(self._tasks):
            try:
                await task.tick()
            except Exception:
                logger.exception("scheduler task %s tick failed", task.name)
