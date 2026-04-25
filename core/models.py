"""Data models for Zulip bot message events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class MessageEvent:
    """A parsed Zulip message event."""

    id: int
    sender_id: int
    sender_email: str
    content: str
    message_type: str  # "private" or "stream"
    stream: str | None
    topic: str | None
    is_me_message: bool
    raw_event: dict[str, Any]


def parse_message_event(event: dict[str, Any]) -> MessageEvent | None:
    """Parse a raw Zulip event dict into a `MessageEvent` or None."""
    if event.get("type") != "message":
        return None
    msg = event.get("message", {})
    msg_type = msg.get("type")
    if msg_type not in ("private", "stream"):
        return None

    msg_id = msg.get("id")
    sender_id = msg.get("sender_id")
    if not isinstance(msg_id, int) or not isinstance(sender_id, int):
        return None

    return MessageEvent(
        id=msg_id,
        sender_id=sender_id,
        sender_email=msg.get("sender_email", ""),
        content=msg.get("content") or "",
        message_type=msg_type,
        stream=msg.get("display_recipient") if msg_type == "stream" else None,
        topic=msg.get("subject") if msg_type == "stream" else None,
        is_me_message=msg.get("is_me_message", False),
        raw_event=event,
    )
