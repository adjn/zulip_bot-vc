from pathlib import Path

import pytest

from config import ConfigManager
from core.context import FeatureContext
from core.models import MessageEvent
from features.admin_controls import AdminControlsFeature, _redact
from storage.db import Storage
from tests.fakes import FakeClient
from utils.scheduling import DeletionScheduler


def _dm(sender_id: int, content: str) -> MessageEvent:
    return MessageEvent(
        id=1,
        sender_id=sender_id,
        sender_email="x@example.com",
        content=content,
        message_type="private",
        stream=None,
        topic=None,
        is_me_message=False,
        raw_event={},
    )


async def _make(
    tmp_path: Path, *, admin_user: bool = True
) -> tuple[AdminControlsFeature, FakeClient, ConfigManager, Storage]:
    fc = FakeClient()
    fc.users[1] = {"is_admin": admin_user, "is_owner": False}
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    cm.load()
    storage = await Storage.open(":memory:")
    sched = DeletionScheduler(delete_fn=fc.delete_message, storage=storage)
    feat = AdminControlsFeature(
        ctx=FeatureContext(client=fc, config_mgr=cm, storage=storage, scheduler=sched)
    )
    return feat, fc, cm, storage


def test_redact_masks_sensitive_keys() -> None:
    src = {"api_key": "abc", "nested": {"token": "x", "ok": 1}, "list": [{"password": "p"}]}
    out = _redact(src)
    assert out["api_key"] == "***REDACTED***"
    assert out["nested"]["token"] == "***REDACTED***"
    assert out["nested"]["ok"] == 1
    assert out["list"][0]["password"] == "***REDACTED***"
    # original untouched
    assert src["api_key"] == "abc"


@pytest.mark.trio
async def test_non_admin_rejected(tmp_path: Path) -> None:
    feat, _fc, _, _storage = await _make(tmp_path, admin_user=False)
    assert await feat.handles(_dm(1, "!config show")) is False


@pytest.mark.trio
async def test_non_bang_dm_ignored(tmp_path: Path) -> None:
    feat, _fc, _, _storage = await _make(tmp_path)
    assert await feat.handles(_dm(1, "hello")) is False


@pytest.mark.trio
async def test_config_show(tmp_path: Path) -> None:
    feat, fc, _, _storage = await _make(tmp_path)
    await feat.handle(_dm(1, "!config show"))
    assert any("yaml" in d.content for d in fc.dms)


@pytest.mark.trio
async def test_anon_set_int_validates(tmp_path: Path) -> None:
    feat, fc, _, _storage = await _make(tmp_path)
    await feat.handle(_dm(1, "!anon set delete_after_minutes notanint"))
    assert any("must be an integer" in d.content for d in fc.dms)


@pytest.mark.trio
async def test_anon_set_enabled_toggles(tmp_path: Path) -> None:
    """`!anon set enabled true|false` must be admin-toggleable.

    The README and copilot-instructions both promise this works; pin it."""
    feat, _fc, cm, _storage = await _make(tmp_path)
    assert cm.get()["anonymous_posting"]["enabled"] is False
    await feat.handle(_dm(1, "!anon set enabled true"))
    assert cm.get()["anonymous_posting"]["enabled"] is True
    await feat.handle(_dm(1, "!anon set enabled false"))
    assert cm.get()["anonymous_posting"]["enabled"] is False


@pytest.mark.trio
async def test_anon_set_quoted_stream_name(tmp_path: Path) -> None:
    feat, _fc, cm, _storage = await _make(tmp_path)
    await feat.handle(_dm(1, '!anon set stream "secret room"'))
    assert cm.get()["anonymous_posting"]["target_stream"] == "secret room"


@pytest.mark.trio
async def test_access_add_yaml_body(tmp_path: Path) -> None:
    feat, _fc, cm, _storage = await _make(tmp_path)
    body = '!access add\nstream: access-requests\ntopic: t\nphrase: "hi"\ntarget_stream: dest\n'
    await feat.handle(_dm(1, body))
    rules = cm.get()["private_access"]["watch_rules"]
    assert any(r.get("target_stream") == "dest" for r in rules)


@pytest.mark.trio
async def test_subscribe_command(tmp_path: Path) -> None:
    feat, fc, _, _storage = await _make(tmp_path)
    await feat.handle(_dm(1, '!subscribe general "anon room"'))
    assert fc.bot_subscriptions == [["general", "anon room"]]


@pytest.mark.trio
async def test_unknown_command(tmp_path: Path) -> None:
    feat, fc, _, _storage = await _make(tmp_path)
    await feat.handle(_dm(1, "!nope"))
    assert any("Unknown command" in d.content for d in fc.dms)


@pytest.mark.trio
async def test_first_token_is_exact_not_prefix(tmp_path: Path) -> None:
    """`!configure` must NOT route to `!config`."""
    feat, fc, _, _storage = await _make(tmp_path)
    await feat.handle(_dm(1, "!configure show"))
    assert any("Unknown command" in d.content for d in fc.dms)


@pytest.mark.trio
async def test_help_lists_all_commands(tmp_path: Path) -> None:
    feat, fc, _, _storage = await _make(tmp_path)
    await feat.handle(_dm(1, "!help"))
    msg = fc.dms[0].content
    for name in ("!help", "!config", "!anon", "!access", "!subscribe"):
        assert name in msg


@pytest.mark.trio
async def test_help_subcommand_shows_per_command_usage(tmp_path: Path) -> None:
    feat, fc, _, _storage = await _make(tmp_path)
    await feat.handle(_dm(1, "!help !anon"))
    assert any("delete_after_minutes" in d.content for d in fc.dms)


@pytest.mark.trio
async def test_help_tolerates_missing_bang(tmp_path: Path) -> None:
    """`!help anon` should work the same as `!help !anon`."""
    feat, fc, _, _storage = await _make(tmp_path)
    await feat.handle(_dm(1, "!help anon"))
    assert any("delete_after_minutes" in d.content for d in fc.dms)


@pytest.mark.trio
async def test_help_unknown_command(tmp_path: Path) -> None:
    feat, fc, _, _storage = await _make(tmp_path)
    await feat.handle(_dm(1, "!help !nope"))
    assert any("Unknown command" in d.content for d in fc.dms)


@pytest.mark.trio
async def test_unknown_command_lists_known(tmp_path: Path) -> None:
    """The 'unknown' reply should enumerate registered commands so a
    user mistyping a name can find the right one."""
    feat, fc, _, _storage = await _make(tmp_path)
    await feat.handle(_dm(1, "!nope"))
    msg = fc.dms[0].content
    assert "Unknown command" in msg
    assert "!anon" in msg and "!config" in msg


@pytest.mark.trio
async def test_anon_set_unknown_field_hints_at_help(tmp_path: Path) -> None:
    feat, fc, _, _storage = await _make(tmp_path)
    await feat.handle(_dm(1, "!anon set wat 1"))
    assert any("!help !anon" in d.content for d in fc.dms)
