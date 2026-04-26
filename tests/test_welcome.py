"""Tests for the welcome feature.

Covers config gating, scheduling, delivery, idempotency, and the
"disable drains pending rows" behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from config import ConfigManager
from core.context import FeatureContext
from core.dispatcher import Dispatcher
from features.welcome.feature import WelcomeFeature
from storage.db import Storage
from tests.fakes import FakeClient


def _cfg(tmp_path: Any, **welcome: Any) -> ConfigManager:
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    cm.load()
    base = cm.get()
    base["welcome"] = {
        "enabled": True,
        "delay_minutes": 10,
        "message": "Hi! Welcome.",
        **welcome,
    }
    cm.update(base)
    return cm


async def _make(tmp_path: Any) -> tuple[WelcomeFeature, FakeClient, Storage, ConfigManager]:
    fc = FakeClient()
    storage = await Storage.open(":memory:")
    cm = _cfg(tmp_path)
    ctx = FeatureContext(client=fc, config_mgr=cm, storage=storage)
    return WelcomeFeature(ctx=ctx), fc, storage, cm


@pytest.mark.trio
async def test_disabled_does_not_schedule(tmp_path: Any) -> None:
    fc = FakeClient()
    storage = await Storage.open(":memory:")
    cm = _cfg(tmp_path, enabled=False)
    ctx = FeatureContext(client=fc, config_mgr=cm, storage=storage)
    feature = WelcomeFeature(ctx=ctx)
    try:
        await feature.on_user_added(42)
        assert await storage.pending_welcome_count() == 0
    finally:
        await storage.close()


@pytest.mark.trio
async def test_enabled_schedules_with_correct_delay(tmp_path: Any) -> None:
    feature, _fc, storage, _cm = await _make(tmp_path)
    try:
        fixed_now = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        with patch("features.welcome.feature._utcnow", return_value=fixed_now):
            await feature.on_user_added(7)
        assert await storage.pending_welcome_count() == 1
        # Should not be due yet.
        assert await storage.claim_due_welcomes(fixed_now) == []
        # Due exactly 10 minutes later.
        assert await storage.claim_due_welcomes(fixed_now + timedelta(minutes=10)) == [7]
    finally:
        await storage.close()


@pytest.mark.trio
async def test_duplicate_add_does_not_double_schedule(tmp_path: Any) -> None:
    """ON CONFLICT DO NOTHING means a re-fired join event preserves the
    *original* deadline rather than pushing it out."""
    feature, _fc, storage, _cm = await _make(tmp_path)
    try:
        early = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        late = early + timedelta(hours=1)
        with patch("features.welcome.feature._utcnow", return_value=early):
            await feature.on_user_added(11)
        with patch("features.welcome.feature._utcnow", return_value=late):
            await feature.on_user_added(11)
        # Still only one row, original deadline (early + 10m) wins.
        assert await storage.pending_welcome_count() == 1
        due_at_original = early + timedelta(minutes=10)
        assert await storage.claim_due_welcomes(due_at_original) == [11]
    finally:
        await storage.close()


@pytest.mark.trio
async def test_tick_delivers_due_welcomes(tmp_path: Any) -> None:
    feature, fc, storage, _cm = await _make(tmp_path)
    try:
        early = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        with patch("features.welcome.feature._utcnow", return_value=early):
            await feature.on_user_added(101)
            await feature.on_user_added(202)
        # Tick at delivery time.
        with patch(
            "features.welcome.feature._utcnow",
            return_value=early + timedelta(minutes=10),
        ):
            await feature.tick()
        assert {dm.user_id for dm in fc.dms} == {101, 202}
        assert all("Welcome" in dm.content for dm in fc.dms)
        assert await storage.pending_welcome_count() == 0
    finally:
        await storage.close()


@pytest.mark.trio
async def test_tick_does_nothing_when_nothing_due(tmp_path: Any) -> None:
    feature, fc, storage, _cm = await _make(tmp_path)
    try:
        early = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        with patch("features.welcome.feature._utcnow", return_value=early):
            await feature.on_user_added(1)
            await feature.tick()  # not due yet
        assert fc.dms == []
        assert await storage.pending_welcome_count() == 1
    finally:
        await storage.close()


@pytest.mark.trio
async def test_disable_at_runtime_drains_pending(tmp_path: Any) -> None:
    """Documented behaviour: flipping enabled=False drops queued welcomes
    rather than queueing them indefinitely."""
    feature, fc, storage, cm = await _make(tmp_path)
    try:
        early = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        with patch("features.welcome.feature._utcnow", return_value=early):
            await feature.on_user_added(99)
        # Disable.
        new_cfg = cm.get()
        new_cfg["welcome"]["enabled"] = False
        cm.update(new_cfg)
        # Tick after deadline -> should drain, NOT deliver.
        with patch(
            "features.welcome.feature._utcnow",
            return_value=early + timedelta(minutes=15),
        ):
            await feature.tick()
        assert fc.dms == []
        assert await storage.pending_welcome_count() == 0
    finally:
        await storage.close()


@pytest.mark.trio
async def test_send_failure_does_not_crash(tmp_path: Any) -> None:
    feature, fc, storage, _cm = await _make(tmp_path)
    try:

        async def bad_send(*_a: Any, **_kw: Any) -> int | None:
            raise RuntimeError("boom")

        fc.send_private_message = bad_send  # type: ignore[method-assign]
        early = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        with patch("features.welcome.feature._utcnow", return_value=early):
            await feature.on_user_added(1)
        with patch(
            "features.welcome.feature._utcnow",
            return_value=early + timedelta(minutes=10),
        ):
            await feature.tick()  # should not raise
        # Row was claimed (no retry policy), pending is 0.
        assert await storage.pending_welcome_count() == 0
    finally:
        await storage.close()


@pytest.mark.trio
async def test_delay_minutes_clamps_negative(tmp_path: Any) -> None:
    fc = FakeClient()
    storage = await Storage.open(":memory:")
    cm = _cfg(tmp_path, delay_minutes=-99)
    ctx = FeatureContext(client=fc, config_mgr=cm, storage=storage)
    feature = WelcomeFeature(ctx=ctx)
    try:
        early = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        with patch("features.welcome.feature._utcnow", return_value=early):
            await feature.on_user_added(5)
        # Clamped to 0 -> due immediately at `early`.
        assert await storage.claim_due_welcomes(early) == [5]
    finally:
        await storage.close()


@pytest.mark.trio
async def test_message_template_substitution(tmp_path: Any) -> None:
    fc = FakeClient()
    storage = await Storage.open(":memory:")
    cm = _cfg(tmp_path, message="hello user {user_id} mention={mention}")
    ctx = FeatureContext(client=fc, config_mgr=cm, storage=storage)
    feature = WelcomeFeature(ctx=ctx)
    try:
        early = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        with patch("features.welcome.feature._utcnow", return_value=early):
            await feature.on_user_added(77)
        with patch(
            "features.welcome.feature._utcnow",
            return_value=early + timedelta(minutes=10),
        ):
            await feature.tick()
        assert len(fc.dms) == 1
        assert "user 77" in fc.dms[0].content
        assert "@_**|77**" in fc.dms[0].content
    finally:
        await storage.close()


@pytest.mark.trio
async def test_message_template_with_unknown_placeholder_is_safe(tmp_path: Any) -> None:
    fc = FakeClient()
    storage = await Storage.open(":memory:")
    cm = _cfg(tmp_path, message="hello {nope}")
    ctx = FeatureContext(client=fc, config_mgr=cm, storage=storage)
    feature = WelcomeFeature(ctx=ctx)
    try:
        early = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        with patch("features.welcome.feature._utcnow", return_value=early):
            await feature.on_user_added(8)
        with patch(
            "features.welcome.feature._utcnow",
            return_value=early + timedelta(minutes=10),
        ):
            await feature.tick()
        # Sent verbatim — feature didn't crash.
        assert fc.dms[0].content == "hello {nope}"
    finally:
        await storage.close()


# ----------------------------------------------------------------------
# Dispatcher integration
# ----------------------------------------------------------------------


@pytest.mark.trio
async def test_dispatcher_routes_realm_user_add(tmp_path: Any) -> None:
    received: list[int] = []

    async def handler(user_id: int) -> None:
        received.append(user_id)

    d = Dispatcher()
    d.register_realm_user_add_handler(handler)
    await d.dispatch_event(
        {"type": "realm_user", "op": "add", "person": {"user_id": 42, "is_bot": False}}
    )
    assert received == [42]


@pytest.mark.trio
async def test_dispatcher_skips_bot_add(tmp_path: Any) -> None:
    received: list[int] = []

    async def handler(user_id: int) -> None:
        received.append(user_id)

    d = Dispatcher()
    d.register_realm_user_add_handler(handler)
    await d.dispatch_event(
        {"type": "realm_user", "op": "add", "person": {"user_id": 42, "is_bot": True}}
    )
    assert received == []


@pytest.mark.trio
async def test_dispatcher_skips_non_add_op(tmp_path: Any) -> None:
    received: list[int] = []

    async def handler(user_id: int) -> None:
        received.append(user_id)

    d = Dispatcher()
    d.register_realm_user_add_handler(handler)
    await d.dispatch_event({"type": "realm_user", "op": "update", "person": {"user_id": 42}})
    assert received == []


@pytest.mark.trio
async def test_dispatcher_handler_exception_does_not_crash(tmp_path: Any) -> None:
    async def boom(_uid: int) -> None:
        raise RuntimeError("boom")

    received: list[int] = []

    async def good(uid: int) -> None:
        received.append(uid)

    d = Dispatcher()
    d.register_realm_user_add_handler(boom)
    d.register_realm_user_add_handler(good)
    await d.dispatch_event(
        {"type": "realm_user", "op": "add", "person": {"user_id": 9, "is_bot": False}}
    )
    assert received == [9]
