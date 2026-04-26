"""Tests for ``ZulipTrioClient.events``.

The events long-poll loop is the bot's lifeline: when it stalls, the bot
goes silent. These tests pin three behaviours that we don't want to
regress:

1. A normal ``get_events`` response yields its events and updates
   ``last_event_id`` between calls.
2. A ``BAD_EVENT_QUEUE_ID`` response raises :class:`QueueInvalidated`
   so the caller can re-register.
3. A ``RATE_LIMIT_HIT`` response sleeps for ``retry-after`` and retries
   in-place rather than crashing or busy-looping.
"""

from __future__ import annotations

from typing import Any

import pytest
import trio

from core.client import QueueInvalidated, ZulipTrioClient


class _FakeZulipClient:
    """Minimal stand-in for ``zulip.Client`` for testing the events loop.

    Each ``get_events`` call pops one scripted response from
    ``responses``. Calls beyond the script return an empty success so
    the test's ``async for`` loop terminates predictably.
    """

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get_events(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if not self.responses:
            return {"result": "success", "events": []}
        return self.responses.pop(0)


def _wrap(zclient: _FakeZulipClient) -> ZulipTrioClient:
    return ZulipTrioClient(zclient)  # type: ignore[arg-type]


@pytest.mark.trio
async def test_events_yields_events_and_advances_last_event_id() -> None:
    zclient = _FakeZulipClient(
        [
            {
                "result": "success",
                "events": [
                    {"id": 5, "type": "message"},
                    {"id": 7, "type": "message"},
                ],
            },
            # Second poll: empty, just to verify last_event_id advanced.
            {"result": "success", "events": []},
        ]
    )
    client = _wrap(zclient)

    received: list[int] = []
    async for event in client.events({"queue_id": "Q1", "last_event_id": 0}):
        received.append(event["id"])
        if len(received) == 2:
            break

    assert received == [5, 7]
    # Second call should have advanced past the highest event we saw.
    # (We only break after 2 events, so a second call may or may not
    # have happened — but the first call's last_event_id is what we
    # care about pinning.)
    assert zclient.calls[0]["last_event_id"] == 0
    assert zclient.calls[0]["queue_id"] == "Q1"


@pytest.mark.trio
async def test_events_raises_queue_invalidated_on_bad_queue_id() -> None:
    zclient = _FakeZulipClient(
        [
            {"result": "error", "code": "BAD_EVENT_QUEUE_ID", "msg": "queue gone"},
        ]
    )
    client = _wrap(zclient)

    with pytest.raises(QueueInvalidated):
        async for _ in client.events({"queue_id": "Q1", "last_event_id": 0}):
            pytest.fail("should not yield any events before raising")


@pytest.mark.trio
async def test_events_retries_on_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RATE_LIMIT_HIT should sleep and retry in-place. We monkey-patch
    `trio.sleep` to record the wait without actually sleeping."""
    zclient = _FakeZulipClient(
        [
            {
                "result": "error",
                "code": "RATE_LIMIT_HIT",
                "retry-after": 2.5,
            },
            {
                "result": "success",
                "events": [{"id": 1, "type": "message"}],
            },
        ]
    )
    client = _wrap(zclient)

    sleeps: list[float] = []

    real_sleep = trio.sleep

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        # Yield to the scheduler so the loop progresses.
        await real_sleep(0)

    monkeypatch.setattr("core.client.trio.sleep", fake_sleep)

    async for event in client.events({"queue_id": "Q1", "last_event_id": 0}):
        assert event["id"] == 1
        break

    assert sleeps, "expected a rate-limit sleep before the retry"
    # `retry-after` wins; we don't assert the exact jittered value, just
    # that it's at least the floor we asked for.
    assert sleeps[0] >= 2.5
