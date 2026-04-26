"""Tests for ``bot_main._event_loop`` recovery and backoff.

These tests pin three reliability behaviours:

1. The loop reconnects after :class:`QueueInvalidated` by calling
   ``register()`` again and resuming dispatch.
2. ``register()`` failures (transient network errors) trigger
   exponential backoff up to ``_REGISTER_BACKOFF_CAP``; the backoff
   counter resets after a successful register.
3. Rapid ``QueueInvalidated`` cycles are rate-limited by
   ``_MIN_REREGISTER_INTERVAL`` so a flapping server doesn't spin the
   loop into a register-storm.

We don't actually sleep — :func:`trio.sleep` is replaced with a no-op
that records the requested duration, so a "60-second backoff" test runs
in microseconds.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import trio

import bot_main
from core.client import QueueInvalidated


class _FakeEventClient:
    """Stand-in for ``ZulipTrioClient`` driven by a scenario script.

    ``register_results`` is consumed in order; each entry is either a
    queue dict or an :class:`Exception` instance to raise.

    ``events_scenarios`` is a parallel list: one entry per successful
    register call, each entry is a list of either event dicts (yielded)
    or an exception (raised). Once exhausted we raise
    :class:`StopIteration` to terminate the loop in tests.
    """

    def __init__(
        self,
        register_results: list[Any],
        events_scenarios: list[list[Any]],
    ) -> None:
        self.register_results = list(register_results)
        self.events_scenarios = list(events_scenarios)
        self.register_calls = 0

    async def register(self, **kwargs: Any) -> dict[str, Any]:
        if not self.register_results:
            raise _StopTest()
        self.register_calls += 1
        item = self.register_results.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def events(self, queue: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        if not self.events_scenarios:
            raise _StopTest()
        scenario = self.events_scenarios.pop(0)
        for item in scenario:
            if isinstance(item, BaseException):
                raise item
            yield item


class _StopTest(Exception):
    """Sentinel used to break out of the otherwise-infinite event loop."""


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def dispatch_event(self, event: dict[str, Any]) -> None:
        self.events.append(event)


@pytest.fixture
def patched_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> list[float]:
    """Replace ``trio.sleep`` (as used inside bot_main) with a no-op
    that records the requested durations. Returns the list so tests can
    assert on it.
    """
    sleeps: list[float] = []

    real_sleep = trio.sleep

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr("bot_main.trio.sleep", fake_sleep)
    return sleeps


async def _run(client: _FakeEventClient, dispatcher: _RecordingDispatcher) -> None:
    """Drive ``_event_loop`` until our scripted scenario raises ``_StopTest``."""
    with pytest.raises(_StopTest):
        await bot_main._event_loop(client, dispatcher)  # type: ignore[arg-type]


@pytest.mark.trio
async def test_reconnects_after_queue_invalidated(patched_clock: list[float]) -> None:
    client = _FakeEventClient(
        register_results=[
            {"queue_id": "Q1", "last_event_id": 0},
            {"queue_id": "Q2", "last_event_id": 10},
        ],
        events_scenarios=[
            [{"id": 1, "type": "message"}, QueueInvalidated()],
            [{"id": 2, "type": "message"}],
        ],
    )
    dispatcher = _RecordingDispatcher()
    await _run(client, dispatcher)

    # We registered twice, dispatched both events.
    assert client.register_calls == 2
    assert [e["id"] for e in dispatcher.events] == [1, 2]


@pytest.mark.trio
async def test_register_failure_backs_off_then_succeeds(
    patched_clock: list[float],
) -> None:
    client = _FakeEventClient(
        register_results=[
            OSError("network blip"),
            ConnectionError("still down"),
            RuntimeError("server 5xx"),
            {"queue_id": "Q1", "last_event_id": 0},
        ],
        events_scenarios=[
            [{"id": 99, "type": "message"}],
        ],
    )
    dispatcher = _RecordingDispatcher()
    await _run(client, dispatcher)

    assert client.register_calls == 4
    assert [e["id"] for e in dispatcher.events] == [99]
    # Three failures → three backoff sleeps before the success.
    backoff_sleeps = [s for s in patched_clock if s >= bot_main._REGISTER_BACKOFF_BASE]
    assert len(backoff_sleeps) >= 3
    # Exponential ramp: 1, 2, 4 (clamped by cap).
    assert backoff_sleeps[0] == pytest.approx(bot_main._REGISTER_BACKOFF_BASE)
    assert backoff_sleeps[1] == pytest.approx(bot_main._REGISTER_BACKOFF_BASE * 2)
    assert backoff_sleeps[2] == pytest.approx(bot_main._REGISTER_BACKOFF_BASE * 4)


@pytest.mark.trio
async def test_backoff_resets_after_successful_register(
    patched_clock: list[float],
) -> None:
    """A failure → success → failure sequence should NOT carry the
    backoff counter across the success."""
    client = _FakeEventClient(
        register_results=[
            OSError("blip 1"),
            OSError("blip 2"),
            {"queue_id": "Q1", "last_event_id": 0},
            OSError("blip 3"),
            {"queue_id": "Q2", "last_event_id": 5},
        ],
        events_scenarios=[
            [QueueInvalidated()],
            [{"id": 1, "type": "message"}],
        ],
    )
    dispatcher = _RecordingDispatcher()
    await _run(client, dispatcher)

    # Filter to only the register-backoff sleeps (>= base). The reregister
    # interval sleep is < base, so it stays out of this list.
    backoff_sleeps = [s for s in patched_clock if s >= bot_main._REGISTER_BACKOFF_BASE]
    # First burst: 1, 2 (two failures); then success; second burst: 1
    # (one failure, counter reset). Three sleeps total at >= base.
    assert backoff_sleeps == [
        pytest.approx(bot_main._REGISTER_BACKOFF_BASE),
        pytest.approx(bot_main._REGISTER_BACKOFF_BASE * 2),
        pytest.approx(bot_main._REGISTER_BACKOFF_BASE),
    ]


@pytest.mark.trio
async def test_backoff_caps_at_max(patched_clock: list[float]) -> None:
    """Many sequential failures should asymptote to the cap, not blow up."""
    failures = [OSError(f"f{i}") for i in range(15)]
    client = _FakeEventClient(
        register_results=[*failures, {"queue_id": "Q", "last_event_id": 0}],
        events_scenarios=[[{"id": 1, "type": "message"}]],
    )
    await _run(client, _RecordingDispatcher())

    backoff_sleeps = [s for s in patched_clock if s >= bot_main._REGISTER_BACKOFF_BASE]
    # The backoff is capped, so no individual sleep exceeds the ceiling.
    assert max(backoff_sleeps) <= bot_main._REGISTER_BACKOFF_CAP


@pytest.mark.trio
async def test_min_reregister_interval_after_invalidation(
    patched_clock: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two QueueInvalidated cycles in quick succession should be paced
    apart by ``_MIN_REREGISTER_INTERVAL``."""
    # Fix the clock so "elapsed" is deterministic (zero between calls).
    fake_now = [100.0]

    def fake_current_time() -> float:
        return fake_now[0]

    monkeypatch.setattr("bot_main.trio.current_time", fake_current_time)

    client = _FakeEventClient(
        register_results=[
            {"queue_id": "Q1", "last_event_id": 0},
            {"queue_id": "Q2", "last_event_id": 1},
            {"queue_id": "Q3", "last_event_id": 2},
        ],
        events_scenarios=[
            [QueueInvalidated()],
            [QueueInvalidated()],
            [{"id": 1, "type": "message"}],
        ],
    )
    await _run(client, _RecordingDispatcher())

    # The first register has no prior; it shouldn't add a pacing sleep.
    # The second and third should each add a pacing sleep close to the
    # minimum interval (since elapsed == 0 in the fake clock).
    pacing_sleeps = [s for s in patched_clock if 0 < s <= bot_main._MIN_REREGISTER_INTERVAL + 0.001]
    assert len(pacing_sleeps) >= 2
    for s in pacing_sleeps:
        assert s == pytest.approx(bot_main._MIN_REREGISTER_INTERVAL)
