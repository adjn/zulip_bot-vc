"""Tests for the generic polling :class:`core.scheduler.Scheduler`."""

from __future__ import annotations

import pytest

from core.scheduler import Scheduler


@pytest.mark.trio
async def test_register_and_tick_once_invokes_each_task() -> None:
    calls: list[str] = []

    async def a() -> None:
        calls.append("a")

    async def b() -> None:
        calls.append("b")

    s = Scheduler(poll_interval_seconds=0.01)
    s.register("a", a)
    s.register("b", b)
    await s.tick_once()
    assert calls == ["a", "b"]


@pytest.mark.trio
async def test_failing_tick_is_isolated_from_siblings() -> None:
    """A raise in one tick must not stop other ticks running."""
    calls: list[str] = []

    async def boom() -> None:
        raise RuntimeError("kaboom")

    async def good() -> None:
        calls.append("good")

    s = Scheduler()
    s.register("boom", boom)
    s.register("good", good)
    await s.tick_once()
    assert calls == ["good"]


@pytest.mark.trio
async def test_failing_tick_does_not_remove_task() -> None:
    """A failing tick should still be invoked again on the next tick."""
    n = {"calls": 0}

    async def flaky() -> None:
        n["calls"] += 1
        raise RuntimeError("nope")

    s = Scheduler()
    s.register("flaky", flaky)
    await s.tick_once()
    await s.tick_once()
    assert n["calls"] == 2


@pytest.mark.trio
async def test_register_during_runtime_does_not_break_iteration() -> None:
    """A tick that registers another tick must not blow up the loop."""
    calls: list[str] = []
    s = Scheduler()

    async def adder() -> None:
        calls.append("adder")
        if len(calls) == 1:
            s.register("late", late)

    async def late() -> None:
        calls.append("late")

    s.register("adder", adder)
    await s.tick_once()
    # First tick: only adder runs, then registers `late`. Snapshot
    # semantics mean `late` is NOT called this tick.
    assert calls == ["adder"]
    await s.tick_once()
    assert calls == ["adder", "adder", "late"]


def test_poll_interval_seconds_property() -> None:
    s = Scheduler(poll_interval_seconds=1.5)
    assert s.poll_interval_seconds == 1.5
