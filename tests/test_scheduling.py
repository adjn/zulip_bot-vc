from datetime import UTC, datetime, timedelta

import pytest

from utils.scheduling import DeletionScheduler


class _FakeNow:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


@pytest.mark.trio
async def test_schedule_and_run_once() -> None:
    deleted: list[int] = []

    async def delete_fn(mid: int) -> bool:
        deleted.append(mid)
        return True

    now = _FakeNow(datetime(2025, 1, 1, tzinfo=UTC))
    s = DeletionScheduler(delete_fn=delete_fn, now_fn=now)
    await s.schedule_deletion(message_id=1, delete_after_minutes=10)
    await s.schedule_deletion(message_id=2, delete_after_minutes=20)

    # Not due yet
    await s._run_once()
    assert deleted == []
    assert s.pending_count() == 2

    # Advance past first deadline only
    now.t = now.t + timedelta(minutes=15)
    await s._run_once()
    assert deleted == [1]
    assert s.pending_count() == 1

    # Advance past second
    now.t = now.t + timedelta(minutes=10)
    await s._run_once()
    assert deleted == [1, 2]
    assert s.pending_count() == 0


@pytest.mark.trio
async def test_failed_delete_is_not_retried() -> None:
    calls: list[int] = []

    async def delete_fn(mid: int) -> bool:
        calls.append(mid)
        return False

    now = _FakeNow(datetime(2025, 1, 1, tzinfo=UTC))
    s = DeletionScheduler(delete_fn=delete_fn, now_fn=now)
    await s.schedule_deletion(message_id=99, delete_after_minutes=0)
    await s._run_once()
    await s._run_once()
    assert calls == [99]
    assert s.pending_count() == 0


@pytest.mark.trio
async def test_reschedule_replaces_existing() -> None:
    deleted: list[int] = []

    async def delete_fn(mid: int) -> bool:
        deleted.append(mid)
        return True

    now = _FakeNow(datetime(2025, 1, 1, tzinfo=UTC))
    s = DeletionScheduler(delete_fn=delete_fn, now_fn=now)
    await s.schedule_deletion(message_id=7, delete_after_minutes=5)
    await s.schedule_deletion(message_id=7, delete_after_minutes=60)

    now.t = now.t + timedelta(minutes=10)
    await s._run_once()
    assert deleted == []  # rescheduled to +60, not yet due

    now.t = now.t + timedelta(minutes=60)
    await s._run_once()
    assert deleted == [7]


@pytest.mark.trio
async def test_delete_exception_does_not_crash_loop() -> None:
    async def delete_fn(_mid: int) -> bool:
        raise RuntimeError("kaboom")

    now = _FakeNow(datetime(2025, 1, 1, tzinfo=UTC))
    s = DeletionScheduler(delete_fn=delete_fn, now_fn=now)
    await s.schedule_deletion(message_id=5, delete_after_minutes=0)
    await s._run_once()
    assert s.pending_count() == 0
