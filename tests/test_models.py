from core.models import parse_message_event


def _msg(**overrides: object) -> dict:
    base: dict = {
        "type": "message",
        "message": {
            "id": 1,
            "sender_id": 42,
            "sender_email": "a@example.com",
            "content": "hi",
            "type": "private",
            "is_me_message": False,
        },
    }
    base["message"].update(overrides)
    return base


def test_parse_private_ok() -> None:
    ev = parse_message_event(_msg())
    assert ev is not None
    assert ev.id == 1
    assert ev.sender_id == 42
    assert ev.message_type == "private"
    assert ev.stream is None and ev.topic is None


def test_parse_stream_ok() -> None:
    ev = parse_message_event(_msg(type="stream", display_recipient="general", subject="hello"))
    assert ev is not None
    assert ev.message_type == "stream"
    assert ev.stream == "general"
    assert ev.topic == "hello"


def test_rejects_non_message_event() -> None:
    assert parse_message_event({"type": "presence"}) is None


def test_rejects_unsupported_message_type() -> None:
    assert parse_message_event(_msg(type="huddle")) is None


def test_rejects_non_int_sender_id() -> None:
    assert parse_message_event(_msg(sender_id="42")) is None


def test_rejects_non_int_id() -> None:
    assert parse_message_event(_msg(id="1")) is None


def test_missing_content_becomes_empty_string() -> None:
    ev = parse_message_event(_msg(content=None))
    assert ev is not None
    assert ev.content == ""
