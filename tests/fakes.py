"""In-memory fakes used by tests.

`FakeClient` matches the shape of `core.client.ClientProtocol`; features
under test depend only on that surface, so this fake is sufficient.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SentDM:
    user_id: int
    content: str
    message_id: int


@dataclass
class SentStream:
    stream: str
    topic: str
    content: str
    message_id: int


@dataclass
class FakeClient:
    """A minimal in-memory Zulip client matching ``ClientProtocol``."""

    next_message_id: int = 1000
    dms: list[SentDM] = field(default_factory=list)
    stream_msgs: list[SentStream] = field(default_factory=list)
    reactions: list[tuple[int, str]] = field(default_factory=list)
    deleted: list[int] = field(default_factory=list)
    user_subscriptions: list[tuple[int, list[str]]] = field(default_factory=list)
    bot_subscriptions: list[list[str]] = field(default_factory=list)
    users: dict[int, dict[str, Any]] = field(default_factory=dict)
    delete_should_fail: bool = False

    def _next_id(self) -> int:
        self.next_message_id += 1
        return self.next_message_id

    async def send_private_message(self, to_user_id: int, content: str) -> int | None:
        mid = self._next_id()
        self.dms.append(SentDM(to_user_id, content, mid))
        return mid

    async def send_stream_message(self, stream: str, topic: str, content: str) -> int | None:
        mid = self._next_id()
        self.stream_msgs.append(SentStream(stream, topic, content, mid))
        return mid

    async def react_to_message(self, message_id: int, emoji_name: str) -> None:
        self.reactions.append((message_id, emoji_name))

    async def add_user_subscriptions(self, user_id: int, streams: Iterable[str]) -> None:
        self.user_subscriptions.append((user_id, list(streams)))

    async def subscribe_bot_to_streams(self, streams: Iterable[str]) -> dict[str, Any]:
        names = list(streams)
        self.bot_subscriptions.append(names)
        return {
            "result": "success",
            "subscribed": {"bot": names},
            "already_subscribed": {},
        }

    async def delete_message(self, message_id: int) -> bool:
        if self.delete_should_fail:
            return False
        self.deleted.append(message_id)
        return True

    async def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        return self.users.get(user_id)
