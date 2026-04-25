"""SQLite-backed durable storage for the bot.

Why SQLite + stdlib :mod:`sqlite3`:

* Zero new dependencies; trio's :func:`trio.to_thread.run_sync` is enough
  to keep blocking calls off the trio task scheduler (same pattern we
  already use in ``core/client.py``).
* :mod:`aiosqlite` is asyncio-native; using it here would force a
  trio↔asyncio bridge for no real benefit at this scale.

What's persisted here:

* ``scheduled_deletions`` -- so the auto-delete privacy contract for
  anonymous posts survives a bot restart.
* ``pending_confirmations`` -- a user's in-flight anonymous post awaiting
  their ``SEND`` / ``CANCEL`` reply.
* ``cooldowns`` -- per-sender ``last_post_at`` for rate limiting.

What's deliberately *not* persisted (yet):

* Audit log of admin actions -- separate concern, future PR.
* Event-queue checkpoint (``queue_id`` / ``last_event_id``) -- needs
  careful re-register-with-old-queue logic, future PR.
* Role cache -- 60s TTL, in-memory is fine.

Concurrency model:

* Trio is single-threaded; we serialise DB calls onto a worker thread
  via :func:`trio.to_thread.run_sync` (see :meth:`_run`).
* WAL mode is enabled so readers never block writers (defence in depth;
  we don't currently have parallel readers).
* A single :class:`sqlite3.Connection` is shared. ``check_same_thread``
  is ``False`` because :mod:`trio.to_thread` may dispatch to different
  worker threads, but we add a :class:`trio.Lock` around every call to
  keep the access pattern serialised from trio's perspective.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

import trio

logger = logging.getLogger(__name__)


# Bumped whenever the schema changes. Migrations live in
# :func:`_apply_migrations` and are idempotent.
SCHEMA_VERSION = 1


_T = TypeVar("_T")


def _iso(dt: datetime) -> str:
    """Serialise a datetime as an ISO-8601 UTC string.

    Naive datetimes are interpreted as UTC. We store as text rather than
    SQLite's native types because SQLite's date handling is famously
    fiddly and ISO strings sort correctly lexicographically.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _from_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


class Storage:
    """Async wrapper around a single SQLite connection.

    Construct via :meth:`open` (which runs migrations and configures
    pragmas) and dispose via :meth:`close`.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        # Serialises trio-side access. Writes hit the same connection;
        # WAL mode means readers don't block, but the lock keeps the
        # access pattern simple and predictable.
        self._lock = trio.Lock()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    @classmethod
    async def open(cls, path: str | Path) -> Storage:
        """Open (or create) the SQLite file at *path* and run migrations.

        Pass ``":memory:"`` for an ephemeral test database.
        """

        def _open_sync() -> sqlite3.Connection:
            if str(path) != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(path),
                # We serialise via trio's to_thread + a trio.Lock, so
                # cross-thread sharing of this connection is safe even
                # though sqlite3 disallows it by default.
                check_same_thread=False,
                isolation_level=None,  # autocommit; we open txns explicitly
            )
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA synchronous = NORMAL")
            _apply_migrations(conn)
            return conn

        conn = await trio.to_thread.run_sync(_open_sync)
        return cls(conn)

    async def close(self) -> None:
        await self._run(self._conn.close)

    # ------------------------------------------------------------------
    # threading helper
    # ------------------------------------------------------------------

    async def _run(self, fn: Callable[[], _T]) -> _T:
        """Run *fn* on a worker thread, serialised by ``self._lock``.

        ``abandon_on_cancel`` is left at its default of ``False``: SQLite
        statements complete in milliseconds, and abandoning a write
        mid-flight could leave a half-open transaction. Cancellation is
        therefore observed at the next await rather than instantaneously.
        """
        async with self._lock:
            return await trio.to_thread.run_sync(fn)

    # ------------------------------------------------------------------
    # scheduled_deletions
    # ------------------------------------------------------------------

    async def schedule_deletion(self, message_id: int, delete_at: datetime) -> None:
        ts = _iso(delete_at)

        def _do() -> None:
            self._conn.execute(
                "INSERT INTO scheduled_deletions (message_id, delete_at) VALUES (?, ?)"
                " ON CONFLICT(message_id) DO UPDATE SET delete_at = excluded.delete_at",
                (message_id, ts),
            )

        await self._run(_do)

    async def cancel_deletion(self, message_id: int) -> None:
        def _do() -> None:
            self._conn.execute(
                "DELETE FROM scheduled_deletions WHERE message_id = ?",
                (message_id,),
            )

        await self._run(_do)

    async def claim_due_deletions(self, now: datetime) -> list[int]:
        """Atomically take ownership of every deletion due at *now*.

        Returns the list of ``message_id``\\ s claimed; rows are removed
        from the table as part of the same transaction. The caller is
        responsible for performing the actual API delete and for logging
        if it fails -- we don't keep a "tried but failed" state today,
        matching the pre-persistence in-memory behaviour.
        """
        ts = _iso(now)

        def _do() -> list[int]:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN IMMEDIATE")
                cur.execute(
                    "SELECT message_id FROM scheduled_deletions WHERE delete_at <= ?",
                    (ts,),
                )
                ids = [row[0] for row in cur.fetchall()]
                if ids:
                    cur.executemany(
                        "DELETE FROM scheduled_deletions WHERE message_id = ?",
                        [(i,) for i in ids],
                    )
                cur.execute("COMMIT")
                return ids
            except Exception:
                cur.execute("ROLLBACK")
                raise
            finally:
                cur.close()

        return await self._run(_do)

    async def pending_deletion_count(self) -> int:
        def _do() -> int:
            cur = self._conn.execute("SELECT COUNT(*) FROM scheduled_deletions")
            (n,) = cur.fetchone()
            return int(n)

        return await self._run(_do)

    # ------------------------------------------------------------------
    # pending_confirmations
    # ------------------------------------------------------------------

    async def upsert_pending(
        self,
        *,
        sender_id: int,
        original_content: str,
        confirmation_message_id: int | None,
        expires_at: datetime,
    ) -> None:
        ts = _iso(expires_at)

        def _do() -> None:
            self._conn.execute(
                "INSERT INTO pending_confirmations "
                "  (sender_id, original_content, confirmation_message_id, expires_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(sender_id) DO UPDATE SET"
                "   original_content = excluded.original_content,"
                "   confirmation_message_id = excluded.confirmation_message_id,"
                "   expires_at = excluded.expires_at",
                (sender_id, original_content, confirmation_message_id, ts),
            )

        await self._run(_do)

    async def fetch_pending(
        self, sender_id: int, *, now: datetime
    ) -> tuple[str, int | None] | None:
        """Return ``(original_content, confirmation_message_id)`` if a
        non-expired pending confirmation exists for *sender_id*, else
        ``None``.

        Expired rows are eagerly deleted as a side effect so that a
        single ``handle()`` call doesn't have to issue a separate eviction
        query first.
        """
        ts = _iso(now)

        def _do() -> tuple[str, int | None] | None:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    "SELECT original_content, confirmation_message_id, expires_at "
                    "FROM pending_confirmations WHERE sender_id = ?",
                    (sender_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                content, conf_id, expires_at = row
                if expires_at <= ts:
                    cur.execute(
                        "DELETE FROM pending_confirmations WHERE sender_id = ?",
                        (sender_id,),
                    )
                    return None
                return (content, conf_id)
            finally:
                cur.close()

        return await self._run(_do)

    async def pop_pending(self, sender_id: int) -> tuple[str, int | None] | None:
        """Atomically fetch and delete a pending confirmation (any TTL).

        Used by SEND / CANCEL where we want to consume the pending row
        regardless of whether it has technically expired between the
        previous handler tick and now.
        """

        def _do() -> tuple[str, int | None] | None:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN IMMEDIATE")
                cur.execute(
                    "SELECT original_content, confirmation_message_id "
                    "FROM pending_confirmations WHERE sender_id = ?",
                    (sender_id,),
                )
                row = cur.fetchone()
                if row is None:
                    cur.execute("COMMIT")
                    return None
                cur.execute(
                    "DELETE FROM pending_confirmations WHERE sender_id = ?",
                    (sender_id,),
                )
                cur.execute("COMMIT")
                return (row[0], row[1])
            except Exception:
                cur.execute("ROLLBACK")
                raise
            finally:
                cur.close()

        return await self._run(_do)

    async def evict_expired_pendings(self, now: datetime) -> int:
        ts = _iso(now)

        def _do() -> int:
            cur = self._conn.execute(
                "DELETE FROM pending_confirmations WHERE expires_at <= ?",
                (ts,),
            )
            return cur.rowcount

        return await self._run(_do)

    # ------------------------------------------------------------------
    # cooldowns
    # ------------------------------------------------------------------

    async def upsert_cooldown(self, sender_id: int, last_post_at: datetime) -> None:
        ts = _iso(last_post_at)

        def _do() -> None:
            self._conn.execute(
                "INSERT INTO cooldowns (sender_id, last_post_at) VALUES (?, ?)"
                " ON CONFLICT(sender_id) DO UPDATE SET last_post_at = excluded.last_post_at",
                (sender_id, ts),
            )

        await self._run(_do)

    async def fetch_cooldown(self, sender_id: int) -> datetime | None:
        def _do() -> datetime | None:
            cur = self._conn.execute(
                "SELECT last_post_at FROM cooldowns WHERE sender_id = ?",
                (sender_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return _from_iso(row[0])

        return await self._run(_do)


# ----------------------------------------------------------------------
# migrations
# ----------------------------------------------------------------------


def _current_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    cur = conn.execute("SELECT version FROM schema_version")
    row = cur.fetchone()
    return int(row[0]) if row else 0


def _set_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply any pending schema migrations.

    Idempotent: running it twice in a row is a no-op. Each numbered
    migration runs inside its own transaction so a partially-applied
    schema can't end up persisted.
    """
    version = _current_version(conn)
    if version == SCHEMA_VERSION:
        return
    if version > SCHEMA_VERSION:
        # Forward-compat hatch: refuse to start against a newer schema
        # rather than silently downgrading and losing data.
        raise RuntimeError(
            f"Database schema version {version} is newer than this code "
            f"supports ({SCHEMA_VERSION}); refusing to start."
        )

    if version < 1:
        conn.execute("BEGIN")
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS scheduled_deletions ("
                "  message_id INTEGER PRIMARY KEY,"
                "  delete_at TEXT NOT NULL"
                ")"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_scheduled_deletions_delete_at "
                "  ON scheduled_deletions(delete_at)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS pending_confirmations ("
                "  sender_id INTEGER PRIMARY KEY,"
                "  original_content TEXT NOT NULL,"
                "  confirmation_message_id INTEGER,"
                "  expires_at TEXT NOT NULL"
                ")"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS cooldowns ("
                "  sender_id INTEGER PRIMARY KEY,"
                "  last_post_at TEXT NOT NULL"
                ")"
            )
            _set_version(conn, 1)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        version = 1

    logger.info("Database migrated to schema version %s", version)
