from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from storage.db import Storage
from utils.scheduling import DeletionScheduler


class _FakeNow:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


async def _fresh_scheduler(delete_fn, now: _FakeNow) -> tuple[DeletionScheduler, Storage]:
    storage = await Storage.open(":memory:")
    return DeletionScheduler(delete_fn=delete_fn, storage=storage, now_fn=now), storage


@pytest.mark.trio
async def test_schedule_and_run_once() -> None:
    deleted: list[int] = []

    async def delete_fn(mid: int) -> bool:
        deleted.append(mid)
        return True

    now = _FakeNow(datetime(2025, 1, 1, tzinfo=UTC))
    s, storage = await _fresh_scheduler(delete_fn, now)
    try:
        await s.schedule_deletion(message_id=1, delete_after_minutes=10)
        await s.schedule_deletion(message_id=2, delete_after_minutes=20)

        # Not due yet
        await s.tick()
        assert deleted == []
        assert await s.pending_count() == 2

        # Advance past first deadline only
        now.t = now.t + timedelta(minutes=15)
        await s.tick()
        assert deleted == [1]
        assert await s.pending_count() == 1

        # Advance past second
        now.t = now.t + timedelta(minutes=10)
        await s.tick()
        assert deleted == [1, 2]
        assert await s.pending_count() == 0
    finally:
        await storage.close()


@pytest.mark.trio
async def test_failed_delete_is_not_retried() -> None:
    calls: list[int] = []

    async def delete_fn(mid: int) -> bool:
        calls.append(mid)
        return False

    now = _FakeNow(datetime(2025, 1, 1, tzinfo=UTC))
    s, storage = await _fresh_scheduler(delete_fn, now)
    try:
        await s.schedule_deletion(message_id=99, delete_after_minutes=0)
        await s.tick()
        await s.tick()
        assert calls == [99]
        assert await s.pending_count() == 0
    finally:
        await storage.close()


@pytest.mark.trio
async def test_reschedule_replaces_existing() -> None:
    deleted: list[int] = []

    async def delete_fn(mid: int) -> bool:
        deleted.append(mid)
        return True

    now = _FakeNow(datetime(2025, 1, 1, tzinfo=UTC))
    s, storage = await _fresh_scheduler(delete_fn, now)
    try:
        await s.schedule_deletion(message_id=7, delete_after_minutes=5)
        await s.schedule_deletion(message_id=7, delete_after_minutes=60)

        now.t = now.t + timedelta(minutes=10)
        await s.tick()
        assert deleted == []  # rescheduled to +60, not yet due

        now.t = now.t + timedelta(minutes=60)
        await s.tick()
        assert deleted == [7]
    finally:
        await storage.close()


@pytest.mark.trio
async def test_delete_exception_does_not_crash_loop() -> None:
    async def delete_fn(_mid: int) -> bool:
        raise RuntimeError("kaboom")

    now = _FakeNow(datetime(2025, 1, 1, tzinfo=UTC))
    s, storage = await _fresh_scheduler(delete_fn, now)
    try:
        await s.schedule_deletion(message_id=5, delete_after_minutes=0)
        await s.tick()
        assert await s.pending_count() == 0
    finally:
        await storage.close()


@pytest.mark.trio
async def test_pending_state_survives_restart(tmp_path: Path) -> None:
    """The whole point of P0.1: a deletion scheduled before a restart
    still fires when the bot comes back up."""
    deleted: list[int] = []

    async def delete_fn(mid: int) -> bool:
        deleted.append(mid)
        return True

    now = _FakeNow(datetime(2025, 1, 1, tzinfo=UTC))
    db_path = tmp_path / "db.sqlite"

    # First "run": schedule a deletion, then close shop.
    s1_storage = await Storage.open(db_path)
    s1 = DeletionScheduler(delete_fn=delete_fn, storage=s1_storage, now_fn=now)
    await s1.schedule_deletion(message_id=42, delete_after_minutes=60)
    await s1_storage.close()

    # Second "run": same DB file, fresh scheduler. Tick after the
    # original deadline -- the deletion should still fire.
    now.t = now.t + timedelta(minutes=120)
    s2_storage = await Storage.open(db_path)
    try:
        s2 = DeletionScheduler(delete_fn=delete_fn, storage=s2_storage, now_fn=now)
        await s2.tick()
        assert deleted == [42]
        assert await s2.pending_count() == 0
    finally:
        await s2_storage.close()
