"""Data models for Zulip bot message events.

Defines MessageEvent dataclass and parsing utilities for handling
Zulip message events.
"""
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class MessageEvent:  # pylint: disable=too-many-instance-attributes
    """Represents a parsed Zulip message event.
    
    Attributes:
        id: Message ID
        sender_id: ID of the user who sent the message
        sender_email: Email of the sender
        content: Message content text
        message_type: Type of message ("private" or "stream")
        stream: Stream name (for stream messages)
        topic: Topic name (for stream messages)
        is_me_message: Whether this is a /me message
        raw_event: Original event dictionary from Zulip API
    """
    id: int
    sender_id: int
    sender_email: str
    content: str
    message_type: str  # "private" or "stream"
    stream: Optional[str]
    topic: Optional[str]
    is_me_message: bool
    raw_event: Dict[str, Any]


def parse_message_event(event: Dict[str, Any]) -> Optional[MessageEvent]:
    """Parse a Zulip event dictionary into a MessageEvent.
    
    Args:
        event: Raw event dictionary from Zulip API
        
    Returns:
        MessageEvent if this is a valid message event, None otherwise
    """
    if event.get("type") != "message":
        return None
    msg = event.get("message", {})
    msg_type = msg.get("type")
    if msg_type not in ("private", "stream"):
        return None

    return MessageEvent(
        id=msg.get("id"),
        sender_id=msg.get("sender_id"),
        sender_email=msg.get("sender_email"),
        content=msg.get("content") or "",
        message_type=msg_type,
        stream=msg.get("display_recipient") if msg_type == "stream" else None,
        topic=msg.get("subject") if msg_type == "stream" else None,
        is_me_message=msg.get("is_me_message", False),
        raw_event=event,
    )
