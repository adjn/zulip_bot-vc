"""Tests for the SQLite storage layer."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from storage.db import SCHEMA_VERSION, Storage, _apply_migrations, _current_version


def _t(minutes: int = 0) -> datetime:
    return datetime(2025, 1, 1, tzinfo=UTC) + timedelta(minutes=minutes)


# ---- lifecycle / schema -------------------------------------------------


@pytest.mark.trio
async def test_open_creates_schema_at_current_version(tmp_path: Path) -> None:
    storage = await Storage.open(tmp_path / "db.sqlite")
    try:
        assert _current_version(storage._conn) == SCHEMA_VERSION
    finally:
        await storage.close()


@pytest.mark.trio
async def test_migration_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite"
    storage = await Storage.open(db_path)
    await storage.close()

    # Re-open should be a no-op (no exception, version unchanged).
    storage2 = await Storage.open(db_path)
    try:
        assert _current_version(storage2._conn) == SCHEMA_VERSION
    finally:
        await storage2.close()


@pytest.mark.trio
async def test_open_creates_parent_directory(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "deeper" / "db.sqlite"
    storage = await Storage.open(nested)
    try:
        assert nested.exists()
    finally:
        await storage.close()


@pytest.mark.trio
async def test_wal_mode_enabled(tmp_path: Path) -> None:
    storage = await Storage.open(tmp_path / "db.sqlite")
    try:
        cur = storage._conn.execute("PRAGMA journal_mode")
        (mode,) = cur.fetchone()
        assert mode.lower() == "wal"
    finally:
        await storage.close()


@pytest.mark.trio
async def test_refuses_newer_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION + 1,))
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="newer than this code"):
        # Open the underlying connection synchronously to bypass the
        # async wrapper and exercise the migration check directly.
        conn2 = sqlite3.connect(str(db_path))
        try:
            _apply_migrations(conn2)
        finally:
            conn2.close()


# ---- scheduled_deletions ------------------------------------------------


@pytest.mark.trio
async def test_schedule_and_claim_due() -> None:
    storage = await Storage.open(":memory:")
    try:
        await storage.schedule_deletion(1, _t(10))
        await storage.schedule_deletion(2, _t(20))
        assert await storage.pending_deletion_count() == 2

        # Nothing due yet
        assert await storage.claim_due_deletions(_t(5)) == []
        assert await storage.pending_deletion_count() == 2

        # First one due
        assert await storage.claim_due_deletions(_t(15)) == [1]
        assert await storage.pending_deletion_count() == 1

        # Second one due
        assert await storage.claim_due_deletions(_t(25)) == [2]
        assert await storage.pending_deletion_count() == 0
    finally:
        await storage.close()


@pytest.mark.trio
async def test_schedule_replaces_existing() -> None:
    storage = await Storage.open(":memory:")
    try:
        await storage.schedule_deletion(7, _t(5))
        await storage.schedule_deletion(7, _t(60))
        assert await storage.pending_deletion_count() == 1

        # Original deadline shouldn't fire
        assert await storage.claim_due_deletions(_t(10)) == []
        # Updated one does
        assert await storage.claim_due_deletions(_t(70)) == [7]
    finally:
        await storage.close()


@pytest.mark.trio
async def test_cancel_deletion() -> None:
    storage = await Storage.open(":memory:")
    try:
        await storage.schedule_deletion(42, _t(10))
        await storage.cancel_deletion(42)
        assert await storage.pending_deletion_count() == 0
        # Cancelling a non-existent row is a no-op
        await storage.cancel_deletion(99)
    finally:
        await storage.close()


@pytest.mark.trio
async def test_deletions_persist_across_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite"
    s1 = await Storage.open(db_path)
    await s1.schedule_deletion(1, _t(60))
    await s1.schedule_deletion(2, _t(120))
    await s1.close()

    s2 = await Storage.open(db_path)
    try:
        assert await s2.pending_deletion_count() == 2
        assert await s2.claim_due_deletions(_t(90)) == [1]
    finally:
        await s2.close()


# ---- pending_confirmations ----------------------------------------------


@pytest.mark.trio
async def test_pending_upsert_and_fetch() -> None:
    storage = await Storage.open(":memory:")
    try:
        await storage.upsert_pending(
            sender_id=7,
            original_content="hello",
            confirmation_message_id=100,
            expires_at=_t(10),
        )
        got = await storage.fetch_pending(7, now=_t(0))
        assert got == ("hello", 100)

        # Upsert overwrites
        await storage.upsert_pending(
            sender_id=7,
            original_content="updated",
            confirmation_message_id=101,
            expires_at=_t(20),
        )
        got = await storage.fetch_pending(7, now=_t(0))
        assert got == ("updated", 101)
    finally:
        await storage.close()


@pytest.mark.trio
async def test_pending_fetch_evicts_expired() -> None:
    storage = await Storage.open(":memory:")
    try:
        await storage.upsert_pending(
            sender_id=7,
            original_content="hello",
            confirmation_message_id=100,
            expires_at=_t(5),
        )
        # After expiry, fetch returns None and removes the row.
        assert await storage.fetch_pending(7, now=_t(10)) is None
        assert await storage.fetch_pending(7, now=_t(0)) is None
    finally:
        await storage.close()


@pytest.mark.trio
async def test_pop_pending() -> None:
    storage = await Storage.open(":memory:")
    try:
        await storage.upsert_pending(
            sender_id=7,
            original_content="hello",
            confirmation_message_id=100,
            expires_at=_t(10),
        )
        popped = await storage.pop_pending(7)
        assert popped == ("hello", 100)
        # Second pop is None
        assert await storage.pop_pending(7) is None
    finally:
        await storage.close()


@pytest.mark.trio
async def test_evict_expired_pendings_bulk() -> None:
    storage = await Storage.open(":memory:")
    try:
        await storage.upsert_pending(
            sender_id=1,
            original_content="a",
            confirmation_message_id=None,
            expires_at=_t(5),
        )
        await storage.upsert_pending(
            sender_id=2,
            original_content="b",
            confirmation_message_id=None,
            expires_at=_t(60),
        )
        evicted = await storage.evict_expired_pendings(_t(10))
        assert evicted == 1
        assert await storage.fetch_pending(1, now=_t(10)) is None
        assert await storage.fetch_pending(2, now=_t(10)) == ("b", None)
    finally:
        await storage.close()


@pytest.mark.trio
async def test_pendings_persist_across_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite"
    s1 = await Storage.open(db_path)
    await s1.upsert_pending(
        sender_id=7,
        original_content="hello",
        confirmation_message_id=100,
        expires_at=_t(60),
    )
    await s1.close()

    s2 = await Storage.open(db_path)
    try:
        assert await s2.fetch_pending(7, now=_t(0)) == ("hello", 100)
    finally:
        await s2.close()


# ---- cooldowns ----------------------------------------------------------


@pytest.mark.trio
async def test_cooldown_round_trip() -> None:
    storage = await Storage.open(":memory:")
    try:
        assert await storage.fetch_cooldown(7) is None
        await storage.upsert_cooldown(7, _t(0))
        assert await storage.fetch_cooldown(7) == _t(0)
        # Update overwrites
        await storage.upsert_cooldown(7, _t(60))
        assert await storage.fetch_cooldown(7) == _t(60)
    finally:
        await storage.close()


@pytest.mark.trio
async def test_cooldowns_persist_across_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite"
    s1 = await Storage.open(db_path)
    await s1.upsert_cooldown(7, _t(60))
    await s1.close()

    s2 = await Storage.open(db_path)
    try:
        assert await s2.fetch_cooldown(7) == _t(60)
    finally:
        await s2.close()
