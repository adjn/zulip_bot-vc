from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class MessageEvent:
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