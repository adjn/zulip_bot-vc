from pathlib import Path

import pytest

from config import ConfigManager
from core.models import MessageEvent
from features.anonymous_posting import (
    AnonymousPostingFeature,
    _escape_for_code_fence,
    _scrub_wildcards,
)
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


def _make_feature(
    tmp_path: Path,
) -> tuple[AnonymousPostingFeature, FakeClient, DeletionScheduler]:
    fc = FakeClient()
    cm = _enabled_cm(tmp_path)
    sched = DeletionScheduler(delete_fn=fc.delete_message)
    feat = AnonymousPostingFeature(client=fc, config_mgr=cm, scheduler=sched)
    return feat, fc, sched


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
    feat, _fc, _ = _make_feature(tmp_path)
    feat.config_mgr.get()["anonymous_posting"]["enabled"] = False
    assert await feat.handles(_event(1, "hello")) is False


@pytest.mark.trio
async def test_admin_dms_are_not_handled(tmp_path: Path) -> None:
    feat, _fc, _ = _make_feature(tmp_path)
    assert await feat.handles(_event(1, "!config show")) is False


@pytest.mark.trio
async def test_full_send_flow(tmp_path: Path) -> None:
    feat, fc, sched = _make_feature(tmp_path)
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
    assert sched.pending_count() == 2


@pytest.mark.trio
async def test_cancel_flow(tmp_path: Path) -> None:
    feat, fc, sched = _make_feature(tmp_path)
    await feat.handle(_event(7, "secret thing"))
    await feat.handle(_event(7, "CANCEL"))
    # No stream msg
    assert fc.stream_msgs == []
    # Cancel ack DM (in addition to the original prompt)
    assert any("Cancelled" in d.content for d in fc.dms)
    # Prompt deletion scheduled
    assert sched.pending_count() == 1


@pytest.mark.trio
async def test_unknown_input_clears_pending(tmp_path: Path) -> None:
    feat, fc, _ = _make_feature(tmp_path)
    await feat.handle(_event(7, "first message"))
    await feat.handle(_event(7, "wat"))
    assert any("Unknown input" in d.content for d in fc.dms)
    assert 7 not in feat._pending


@pytest.mark.trio
async def test_oversize_rejected(tmp_path: Path) -> None:
    feat, fc, _ = _make_feature(tmp_path)
    feat.config_mgr.get()["anonymous_posting"]["max_content_length"] = 10
    await feat.handle(_event(7, "x" * 100))
    assert any("max is 10" in d.content for d in fc.dms)
    assert 7 not in feat._pending


@pytest.mark.trio
async def test_backtick_injection_does_not_break_fence(tmp_path: Path) -> None:
    feat, fc, _ = _make_feature(tmp_path)
    await feat.handle(_event(7, "```\nbreak\n```"))
    prompt = fc.dms[0].content
    # Exactly two ``` runs from our fence; nothing in between.
    assert prompt.count("```") == 2


@pytest.mark.trio
async def test_wildcard_mention_scrub_on_send(tmp_path: Path) -> None:
    feat, fc, _ = _make_feature(tmp_path)
    await feat.handle(_event(7, "hi @**all**"))
    await feat.handle(_event(7, "SEND"))
    assert len(fc.stream_msgs) == 1
    assert "@**all**" not in fc.stream_msgs[0].content


@pytest.mark.trio
async def test_pending_ttl_eviction(tmp_path: Path) -> None:
    feat, fc, _ = _make_feature(tmp_path)
    feat.config_mgr.get()["anonymous_posting"]["pending_ttl_minutes"] = 0
    await feat.handle(_event(7, "first"))
    # SEND now lands as a *new* message because the pending entry is expired.
    fc.stream_msgs.clear()
    await feat.handle(_event(7, "SEND"))
    assert fc.stream_msgs == []  # treated as a new submission, then prompted


@pytest.mark.trio
async def test_cooldown_blocks_rapid_second_post(tmp_path: Path) -> None:
    feat, fc, _ = _make_feature(tmp_path)
    feat.config_mgr.get()["anonymous_posting"]["min_seconds_between_posts"] = 60
    await feat.handle(_event(7, "first"))
    await feat.handle(_event(7, "SEND"))
    # Now try again immediately
    fc.dms.clear()
    await feat.handle(_event(7, "second"))
    assert any("Please wait" in d.content for d in fc.dms)
