"""Tests for the command registry."""

from __future__ import annotations

import pytest

from core.commands import Command, CommandContext, CommandRegistry
from core.models import MessageEvent


def _ctx(*tokens: str, body: str = "") -> CommandContext:
    event = MessageEvent(
        id=1,
        sender_id=1,
        sender_email="x@example.com",
        content=" ".join(tokens),
        message_type="private",
        stream=None,
        topic=None,
        is_me_message=False,
        raw_event={},
    )
    return CommandContext(event=event, tokens=list(tokens), body=body)


@pytest.mark.trio
async def test_dispatch_routes_to_registered_handler() -> None:
    calls: list[CommandContext] = []

    async def handler(ctx: CommandContext) -> None:
        calls.append(ctx)

    reg = CommandRegistry()
    reg.register(Command(name="!foo", summary="s", usage="u", handler=handler))

    assert await reg.dispatch(_ctx("!foo", "arg")) is True
    assert len(calls) == 1
    assert calls[0].tokens == ["!foo", "arg"]


@pytest.mark.trio
async def test_dispatch_unknown_command_returns_false() -> None:
    reg = CommandRegistry()
    assert await reg.dispatch(_ctx("!nope")) is False


@pytest.mark.trio
async def test_dispatch_empty_tokens_returns_false() -> None:
    reg = CommandRegistry()
    assert await reg.dispatch(_ctx()) is False


@pytest.mark.trio
async def test_exact_match_does_not_match_prefix() -> None:
    """`!configure` must NOT route to `!config` -- registry is exact-match only."""
    calls: list[str] = []

    async def handler(_ctx: CommandContext) -> None:
        calls.append("config")

    reg = CommandRegistry()
    reg.register(Command(name="!config", summary="s", usage="u", handler=handler))

    assert await reg.dispatch(_ctx("!configure")) is False
    assert calls == []


@pytest.mark.trio
async def test_register_overwrites_existing_name() -> None:
    """Re-registering a name swaps the handler -- handy for tests."""
    calls: list[str] = []

    async def first(_ctx: CommandContext) -> None:
        calls.append("first")

    async def second(_ctx: CommandContext) -> None:
        calls.append("second")

    reg = CommandRegistry()
    reg.register(Command(name="!x", summary="s", usage="u", handler=first))
    reg.register(Command(name="!x", summary="s", usage="u", handler=second))

    await reg.dispatch(_ctx("!x"))
    assert calls == ["second"]


def test_format_overview_lists_commands_sorted() -> None:
    async def noop(_ctx: CommandContext) -> None: ...

    reg = CommandRegistry()
    reg.register(Command(name="!zeta", summary="last", usage="u", handler=noop))
    reg.register(Command(name="!alpha", summary="first", usage="u", handler=noop))

    out = reg.format_overview()
    assert out.index("!alpha") < out.index("!zeta")
    assert "first" in out and "last" in out
    assert "!help <command>" in out


def test_format_overview_empty_registry() -> None:
    reg = CommandRegistry()
    assert "No commands" in reg.format_overview()


def test_format_command_help_known_command() -> None:
    async def noop(_ctx: CommandContext) -> None: ...

    reg = CommandRegistry()
    reg.register(Command(name="!foo", summary="does foo", usage="`!foo bar`", handler=noop))

    text = reg.format_command_help("!foo")
    assert "!foo" in text
    assert "does foo" in text
    assert "!foo bar" in text


def test_format_command_help_unknown_command() -> None:
    reg = CommandRegistry()
    text = reg.format_command_help("!nope")
    assert "Unknown command" in text
    assert "!nope" in text


def test_names_returns_sorted() -> None:
    async def noop(_ctx: CommandContext) -> None: ...

    reg = CommandRegistry()
    for n in ("!c", "!a", "!b"):
        reg.register(Command(name=n, summary="s", usage="u", handler=noop))
    assert reg.names() == ["!a", "!b", "!c"]
