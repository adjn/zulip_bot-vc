"""Tests for :mod:`core.status`.

The handler-level integration is exercised in
``tests/test_admin_controls.py``; here we lock in the pure data
gathering and the renderer so the rendered string can change without
breaking storage assumptions, and vice versa.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from config import ConfigManager
from core.context import FeatureContext
from core.status import StatusReport, _fmt_uptime
from storage.db import SCHEMA_VERSION, Storage
from tests.fakes import FakeClient


def _cm(tmp_path: Path) -> ConfigManager:
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    cm.load()
    return cm


@pytest.mark.trio
async def test_gather_with_full_ctx(tmp_path: Path) -> None:
    fc = FakeClient()
    cm = _cm(tmp_path)
    cfg = cm.get()
    cfg["admin"]["super_admin_user_ids"] = [1, 2, 3]
    cfg["audit"] = {"enabled": True, "stream": "ops", "topic": "audit"}
    storage = await Storage.open(":memory:")
    started = datetime.now(UTC) - timedelta(hours=1, minutes=30)
    ctx = FeatureContext(
        client=fc,
        config_mgr=cm,
        storage=storage,
        started_at=started,
        bot_user_id=42,
    )

    report = await StatusReport.gather(ctx)

    assert report.schema_version == SCHEMA_VERSION
    assert report.pending_deletions == 0
    assert report.audit_enabled is True
    assert report.audit_broadcast_stream == "ops"
    assert report.super_admin_count == 3
    assert report.bot_user_id == 42
    assert report.uptime is not None
    assert report.uptime >= timedelta(hours=1)


@pytest.mark.trio
async def test_gather_without_storage_or_started_at(tmp_path: Path) -> None:
    """Missing optional ctx fields should produce ``None`` placeholders, not crash."""
    ctx = FeatureContext(client=FakeClient(), config_mgr=_cm(tmp_path))
    report = await StatusReport.gather(ctx)
    assert report.uptime is None
    assert report.pending_deletions is None
    # Audit defaults: enabled, no broadcast.
    assert report.audit_enabled is True
    assert report.audit_broadcast_stream is None
    assert report.super_admin_count == 0


def test_render_includes_key_lines() -> None:
    report = StatusReport(
        uptime=timedelta(days=1, hours=2, minutes=3, seconds=4),
        schema_version=2,
        pending_deletions=5,
        audit_enabled=True,
        audit_broadcast_stream="ops",
        super_admin_count=2,
        bot_user_id=99,
    )
    out = report.render()
    assert "1d 2h 3m 4s" in out
    assert "schema version: `2`" in out
    assert "Scheduled deletions pending: 5" in out
    assert "`ops`" in out
    assert "`2`" in out
    assert "`99`" in out


def test_render_handles_missing_fields() -> None:
    report = StatusReport(
        uptime=None,
        schema_version=1,
        pending_deletions=None,
        audit_enabled=False,
        audit_broadcast_stream=None,
        super_admin_count=0,
        bot_user_id=None,
    )
    out = report.render()
    assert "unknown" in out  # uptime
    assert "n/a" in out  # pending deletions
    assert "`disabled`" in out  # broadcast off
    assert "Bot user id: `unknown`" in out


@pytest.mark.parametrize(
    "td, expected",
    [
        (timedelta(seconds=0), "0s"),
        (timedelta(seconds=42), "42s"),
        (timedelta(minutes=5, seconds=7), "5m 7s"),
        (timedelta(hours=2, minutes=0, seconds=30), "2h 0m 30s"),
        (timedelta(days=3, hours=4), "3d 4h 0m 0s"),
    ],
)
def test_fmt_uptime(td: timedelta, expected: str) -> None:
    assert _fmt_uptime(td) == expected


def test_fmt_uptime_handles_negative_clock_skew() -> None:
    """A clock that went backwards must not crash the renderer."""
    assert _fmt_uptime(timedelta(seconds=-10)) == "0s"
