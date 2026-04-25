from utils.matching import normalize_phrase


def test_normalize_phrase_strips_and_lowercases() -> None:
    assert normalize_phrase("  Hello World  ") == "hello world"


def test_normalize_phrase_idempotent() -> None:
    once = normalize_phrase("HELLO")
    twice = normalize_phrase(once)
    assert once == twice == "hello"


def test_normalize_phrase_empty() -> None:
    assert normalize_phrase("   ") == ""
