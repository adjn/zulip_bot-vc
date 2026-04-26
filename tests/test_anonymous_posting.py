from datetime import UTC, datetime
from pathlib import Path

import pytest

from config import ConfigManager
from core.context import FeatureContext
from core.models import MessageEvent
from features.anonymous_posting import (
    AnonymousPostingFeature,
    _escape_for_code_fence,
    _scrub_wildcards,
)
from storage.db import Storage
from tests.fakes import FakeClient
from utils.scheduling import DeletionScheduler


def _event(sender_id: int, content: str, msg_id: int = 1) -> MessageEvent:
    return MessageEvent(
        id=msg_id,
        sender_id=sender_id,
        sender_email="u@example.com",
        content=content,
        message_type="private",
        stream=None,
        topic=None,
        is_me_message=False,
        raw_event={},
    )


def _enabled_cm(tmp_path: Path) -> ConfigManager:
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    cm.load()
    cfg = cm.get()
    cfg["anonymous_posting"]["enabled"] = True
    cfg["anonymous_posting"]["target_stream"] = "anon"
    cfg["anonymous_posting"]["target_topic"] = "t"
    cfg["anonymous_posting"]["min_seconds_between_posts"] = 0
    return cm


async def _make_feature(
    tmp_path: Path,
    *,
    storage: Storage | None = None,
) -> tuple[AnonymousPostingFeature, FakeClient, DeletionScheduler, Storage]:
    fc = FakeClient()
    cm = _enabled_cm(tmp_path)
    s = storage if storage is not None else await Storage.open(":memory:")
    sched = DeletionScheduler(delete_fn=fc.delete_message, storage=s)
    feat = AnonymousPostingFeature(
        ctx=FeatureContext(client=fc, config_mgr=cm, storage=s, scheduler=sched)
    )
    return feat, fc, sched, s


# --- pure helpers --------------------------------------------------------


def test_escape_breaks_triple_backtick_runs() -> None:
    out = _escape_for_code_fence("hello ``` world ```` end")
    assert "```" not in out
    assert "hello" in out and "end" in out


def test_scrub_wildcards_neutralises_known_forms() -> None:
    s = "hi @**all** and @*everyone* and @_**stream**_"
    out = _scrub_wildcards(s)
    assert "@**all**" not in out
    assert "@*everyone*" not in out
    assert "@_**stream**_" not in out


# --- flow ----------------------------------------------------------------


@pytest.mark.trio
async def test_disabled_feature_does_not_handle(tmp_path: Path) -> None:
    feat, _fc, _, storage = await _make_feature(tmp_path)
    try:
        feat.config_mgr.get()["anonymous_posting"]["enabled"] = False
        assert await feat.handles(_event(1, "hello")) is False
    finally:
        await storage.close()


@pytest.mark.trio
async def test_admin_dms_are_not_handled(tmp_path: Path) -> None:
    feat, _fc, _, storage = await _make_feature(tmp_path)
    try:
        assert await feat.handles(_event(1, "!config show")) is False
    finally:
        await storage.close()


@pytest.mark.trio
async def test_full_send_flow(tmp_path: Path) -> None:
    feat, fc, sched, storage = await _make_feature(tmp_path)
    try:
        await feat.handle(_event(7, "I would like to confess"))
        # Confirmation prompt sent
        assert len(fc.dms) == 1
        assert "SEND" in fc.dms[0].content

        await feat.handle(_event(7, "SEND"))
        # Stream message posted
        assert len(fc.stream_msgs) == 1
        assert "Anonymous message" in fc.stream_msgs[0].content
        assert fc.stream_msgs[0].stream == "anon"
        # Deletion scheduled for both the relayed message and the prompt
        assert await sched.pending_count() == 2
    finally:
        await storage.close()


@pytest.mark.trio
async def test_cancel_flow(tmp_path: Path) -> None:
    feat, fc, sched, storage = await _make_feature(tmp_path)
    try:
        await feat.handle(_event(7, "secret thing"))
        await feat.handle(_event(7, "CANCEL"))
        # No stream msg
        assert fc.stream_msgs == []
        # Cancel ack DM (in addition to the original prompt)
        assert any("Cancelled" in d.content for d in fc.dms)
        # Prompt deletion scheduled
        assert await sched.pending_count() == 1
    finally:
        await storage.close()


@pytest.mark.trio
async def test_unknown_input_clears_pending(tmp_path: Path) -> None:
    feat, fc, _, storage = await _make_feature(tmp_path)
    try:
        await feat.handle(_event(7, "first message"))
        await feat.handle(_event(7, "wat"))
        assert any("Unknown input" in d.content for d in fc.dms)
        # Pending should have been popped
        assert await storage.fetch_pending(7, now=datetime.now(UTC)) is None
    finally:
        await storage.close()


@pytest.mark.trio
async def test_oversize_rejected(tmp_path: Path) -> None:
    feat, fc, _, storage = await _make_feature(tmp_path)
    try:
        feat.config_mgr.get()["anonymous_posting"]["max_content_length"] = 10
        await feat.handle(_event(7, "x" * 100))
        assert any("max is 10" in d.content for d in fc.dms)
        assert await storage.fetch_pending(7, now=datetime.now(UTC)) is None
    finally:
        await storage.close()


@pytest.mark.trio
async def test_backtick_injection_does_not_break_fence(tmp_path: Path) -> None:
    feat, fc, _, storage = await _make_feature(tmp_path)
    try:
        await feat.handle(_event(7, "```\nbreak\n```"))
        prompt = fc.dms[0].content
        # Exactly two ``` runs from our fence; nothing in between.
        assert prompt.count("```") == 2
    finally:
        await storage.close()


@pytest.mark.trio
async def test_wildcard_mention_scrub_on_send(tmp_path: Path) -> None:
    feat, fc, _, storage = await _make_feature(tmp_path)
    try:
        await feat.handle(_event(7, "hi @**all**"))
        await feat.handle(_event(7, "SEND"))
        assert len(fc.stream_msgs) == 1
        assert "@**all**" not in fc.stream_msgs[0].content
    finally:
        await storage.close()


@pytest.mark.trio
async def test_pending_ttl_eviction(tmp_path: Path) -> None:
    feat, fc, _, storage = await _make_feature(tmp_path)
    try:
        feat.config_mgr.get()["anonymous_posting"]["pending_ttl_minutes"] = 0
        await feat.handle(_event(7, "first"))
        # SEND now lands as a *new* message because the pending entry is expired.
        fc.stream_msgs.clear()
        await feat.handle(_event(7, "SEND"))
        assert fc.stream_msgs == []  # treated as a new submission, then prompted
    finally:
        await storage.close()


@pytest.mark.trio
async def test_cooldown_blocks_rapid_second_post(tmp_path: Path) -> None:
    feat, fc, _, storage = await _make_feature(tmp_path)
    try:
        feat.config_mgr.get()["anonymous_posting"]["min_seconds_between_posts"] = 60
        await feat.handle(_event(7, "first"))
        await feat.handle(_event(7, "SEND"))
        # Now try again immediately
        fc.dms.clear()
        await feat.handle(_event(7, "second"))
        assert any("Please wait" in d.content for d in fc.dms)
    finally:
        await storage.close()


@pytest.mark.trio
async def test_pending_state_survives_restart(tmp_path: Path) -> None:
    """Pending confirmation persists across simulated restart."""
    db_path = tmp_path / "db.sqlite"
    s1 = await Storage.open(db_path)
    feat1, fc1, _, _ = await _make_feature(tmp_path, storage=s1)
    await feat1.handle(_event(7, "this is a secret"))
    assert len(fc1.dms) == 1  # confirmation prompt
    await s1.close()

    # New process: same DB, fresh client+feature. SEND should relay the
    # original content rather than be treated as a new submission.
    s2 = await Storage.open(db_path)
    try:
        feat2, fc2, _, _ = await _make_feature(tmp_path, storage=s2)
        await feat2.handle(_event(7, "SEND"))
        assert len(fc2.stream_msgs) == 1
        assert "this is a secret" in fc2.stream_msgs[0].content
    finally:
        await s2.close()


@pytest.mark.trio
async def test_cooldown_survives_restart(tmp_path: Path) -> None:
    """A user who just posted can't bypass the cooldown by waiting for a restart."""
    db_path = tmp_path / "db.sqlite"

    s1 = await Storage.open(db_path)
    feat1, _, _, _ = await _make_feature(tmp_path, storage=s1)
    feat1.config_mgr.get()["anonymous_posting"]["min_seconds_between_posts"] = 3600
    await feat1.handle(_event(7, "first"))
    await feat1.handle(_event(7, "SEND"))
    await s1.close()

    s2 = await Storage.open(db_path)
    try:
        feat2, fc2, _, _ = await _make_feature(tmp_path, storage=s2)
        feat2.config_mgr.get()["anonymous_posting"]["min_seconds_between_posts"] = 3600
        await feat2.handle(_event(7, "second attempt"))
        assert any("Please wait" in d.content for d in fc2.dms)
    finally:
        await s2.close()


def test_scrub_wildcards_handles_silent_role_mention() -> None:
    """`@_*role*_` is the silent variant of `@*role*` and was a gap pre-hardening."""
    s = "hi @_*moderators*_ heads up"
    out = _scrub_wildcards(s)
    assert "@_*moderators*_" not in out
    assert "moderators" in out  # text preserved, only the leading @ defanged


@pytest.mark.trio
async def test_empty_message_is_rejected_before_pending_row(tmp_path: Path) -> None:
    """Whitespace-only content shouldn't open a confirmation flow."""
    feat, fc, _sched, storage = await _make_feature(tmp_path)
    try:
        await feat.handle(_event(1, "   \n  "))
        # We expect a single error DM and *no* pending row.
        assert any("empty" in d.content.lower() for d in fc.dms)
        pending = await storage.fetch_pending(1, now=datetime.now(UTC))
        assert pending is None
    finally:
        await storage.close()
