from dataclasses import dataclass, field

import pytest

from core.dispatcher import Dispatcher, FeatureHandler
from core.models import MessageEvent


def _make_event(sender_id: int = 1, content: str = "hi") -> dict:
    return {
        "type": "message",
        "message": {
            "id": 100,
            "sender_id": sender_id,
            "sender_email": "x@example.com",
            "content": content,
            "type": "private",
            "is_me_message": False,
        },
    }


@dataclass
class _Recorder(FeatureHandler):
    name: str = "rec"
    handles_calls: list[MessageEvent] = field(default_factory=list)
    handle_calls: list[MessageEvent] = field(default_factory=list)
    will_handle: bool = True
    raise_on_handle: bool = False

    async def handles(self, event: MessageEvent) -> bool:
        self.handles_calls.append(event)
        return self.will_handle

    async def handle(self, event: MessageEvent) -> None:
        self.handle_calls.append(event)
        if self.raise_on_handle:
            raise RuntimeError("boom")


@pytest.mark.trio
async def test_dispatch_routes_to_features() -> None:
    d = Dispatcher()
    rec = _Recorder()
    d.register_feature(rec)
    await d.dispatch_event(_make_event())
    assert len(rec.handle_calls) == 1


@pytest.mark.trio
async def test_dispatch_drops_self_messages() -> None:
    d = Dispatcher(bot_user_id=42)
    rec = _Recorder()
    d.register_feature(rec)
    await d.dispatch_event(_make_event(sender_id=42))
    assert rec.handle_calls == []
    assert rec.handles_calls == []  # short-circuit before handles()


@pytest.mark.trio
async def test_dispatch_isolates_feature_errors() -> None:
    d = Dispatcher()
    bad = _Recorder(name="bad", raise_on_handle=True)
    good = _Recorder(name="good")
    d.register_feature(bad)
    d.register_feature(good)
    await d.dispatch_event(_make_event())
    # good still ran despite bad raising
    assert len(good.handle_calls) == 1


@pytest.mark.trio
async def test_dispatch_ignores_non_message_events() -> None:
    d = Dispatcher()
    rec = _Recorder()
    d.register_feature(rec)
    await d.dispatch_event({"type": "presence"})
    assert rec.handles_calls == []
