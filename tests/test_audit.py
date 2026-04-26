"""Tests for :mod:`core.audit`.

Covers:

* ``record`` persists rows the storage layer can read back.
* ``record`` is a no-op when ``audit.enabled`` is False.
* Broadcast posts to the configured stream/topic with actor + action.
* Broadcast is skipped when ``audit.stream`` is unset.
* ``recent`` hydrates JSON details and returns newest-first.
* A failing broadcast does not raise to the caller.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config import ConfigManager
from core.audit import AuditLog
from storage.db import Storage
from tests.fakes import FakeClient


def _cm(tmp_path: Path, **audit_overrides: object) -> ConfigManager:
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    cm.load()
    cfg = cm.get()
    cfg.setdefault("audit", {}).update(audit_overrides)
    return cm


async def _audit(tmp_path: Path, **audit_overrides: object) -> tuple[AuditLog, FakeClient, Storage]:
    fc = FakeClient()
    storage = await Storage.open(":memory:")
    cm = _cm(tmp_path, **audit_overrides)
    return AuditLog(storage=storage, config_mgr=cm, client=fc), fc, storage


@pytest.mark.trio
async def test_record_persists_row(tmp_path: Path) -> None:
    audit, _fc, storage = await _audit(tmp_path)
    entry_id = await audit.record(
        "config.anon.set",
        actor_id=1,
        target="anonymous_posting.enabled",
        details={"value": True},
    )
    assert entry_id > 0
    rows = await storage.recent_audit(limit=10)
    assert len(rows) == 1
    assert rows[0]["action"] == "config.anon.set"
    assert rows[0]["actor_id"] == 1
    assert rows[0]["target"] == "anonymous_posting.enabled"
    # details persisted as JSON string
    assert isinstance(rows[0]["details"], str)
    assert "true" in rows[0]["details"]


@pytest.mark.trio
async def test_record_disabled_is_noop(tmp_path: Path) -> None:
    audit, _fc, storage = await _audit(tmp_path, enabled=False)
    entry_id = await audit.record("config.anon.set", actor_id=1)
    assert entry_id == -1
    assert await storage.recent_audit(limit=10) == []


@pytest.mark.trio
async def test_broadcast_when_stream_set(tmp_path: Path) -> None:
    audit, fc, _storage = await _audit(tmp_path, stream="ops", topic="audit")
    await audit.record("bot.subscribe", actor_id=42, details={"requested": ["g"]})
    assert len(fc.stream_msgs) == 1
    msg = fc.stream_msgs[0]
    assert msg.stream == "ops"
    assert msg.topic == "audit"
    assert "bot.subscribe" in msg.content
    assert "actor=`42`" in msg.content


@pytest.mark.trio
async def test_no_broadcast_when_stream_unset(tmp_path: Path) -> None:
    audit, fc, _storage = await _audit(tmp_path)
    await audit.record("bot.subscribe", actor_id=1)
    assert fc.stream_msgs == []


@pytest.mark.trio
async def test_recent_returns_newest_first_with_details(tmp_path: Path) -> None:
    audit, _fc, _storage = await _audit(tmp_path)
    await audit.record("a", actor_id=1, details={"k": 1})
    await audit.record("b", actor_id=2, details={"k": 2})
    await audit.record("c", actor_id=3)
    entries = await audit.recent(limit=10)
    assert [e.action for e in entries] == ["c", "b", "a"]
    assert entries[1].details == {"k": 2}
    assert entries[2].details == {"k": 1}
    assert entries[0].details is None


@pytest.mark.trio
async def test_broadcast_failure_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit, fc, storage = await _audit(tmp_path, stream="ops")

    async def boom(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("zulip down")

    monkeypatch.setattr(fc, "send_stream_message", boom)
    # Should swallow the broadcast error and still return the persisted id.
    entry_id = await audit.record("config.anon.set", actor_id=1)
    assert entry_id > 0
    rows = await storage.recent_audit(limit=10)
    assert len(rows) == 1


@pytest.mark.trio
async def test_long_details_truncated_in_broadcast(tmp_path: Path) -> None:
    audit, fc, _storage = await _audit(tmp_path, stream="ops")
    big = {"blob": "x" * 1000}
    await audit.record("config.anon.set", actor_id=1, details=big)
    assert fc.stream_msgs
    content = fc.stream_msgs[0].content
    # Truncation marker present; full payload preserved in DB.
    assert "..." in content
