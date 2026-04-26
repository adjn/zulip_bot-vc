"""A small command registry for DM-driven admin commands.

Each :class:`Command` carries enough metadata to route DM input *and*
auto-generate per-command and top-level help text. Adding a new admin
command is therefore a one-place change: register a new
:class:`Command`, and ``!help`` learns about it automatically.

Design choices worth flagging for a junior dev reading this:

* Command lookup is exact match on the first whitespace-separated token.
  ``!configfoo`` does *not* route to ``!config``. The collision-prone
  ``startswith`` style is gone.
* The registry doesn't know about authentication, rate limiting, or
  feature flags. Those are the caller's job (see
  ``AdminControlsFeature.handles`` / ``handle``). Keeping the registry
  pure makes it trivial to test.
* Handlers receive a :class:`CommandContext` rather than positional
  arguments. New context fields can be added without touching every
  existing handler signature.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.models import MessageEvent


@dataclass(frozen=True)
class CommandContext:
    """Everything a handler needs to do its job.

    ``event`` is the original :class:`MessageEvent`; ``tokens`` is the
    pre-split (via :mod:`shlex`) command line; ``body`` is the rest of
    the DM after the first line, used by commands like ``!access`` that
    accept a YAML payload.
    """

    event: MessageEvent
    tokens: list[str]
    body: str

    @property
    def sender_id(self) -> int:
        return self.event.sender_id


# A handler is an async function from CommandContext to None. Handlers
# are expected to send their own user-visible reply via the client; the
# registry never touches the wire itself.
CommandHandler = Callable[[CommandContext], Awaitable[None]]


@dataclass(frozen=True)
class Command:
    """A single top-level admin command.

    :param name: full token including the ``!`` prefix, e.g. ``!anon``.
    :param summary: one-line description for the top-level ``!help``
        listing. Kept under ~70 chars by convention.
    :param usage: multi-line usage text shown by ``!help <name>`` and
        on argument errors. Markdown is fine -- this gets sent verbatim
        as a Zulip DM.
    :param handler: async callable invoked when the command matches.
    """

    name: str
    summary: str
    usage: str
    handler: CommandHandler


@dataclass
class CommandRegistry:
    """Holds the set of registered commands and dispatches by name."""

    _commands: dict[str, Command] = field(default_factory=dict, repr=False)

    def register(self, command: Command) -> None:
        """Add a command. Re-registering the same name overwrites it.

        Overwriting is intentional: it makes the registry trivially
        replaceable in tests and avoids subtle ordering bugs if a future
        feature loader registers from multiple modules.
        """
        self._commands[command.name] = command

    def get(self, name: str) -> Command | None:
        return self._commands.get(name)

    def names(self) -> list[str]:
        return sorted(self._commands)

    async def dispatch(self, ctx: CommandContext) -> bool:
        """Route ``ctx`` to the matching handler.

        Returns ``True`` if a handler ran, ``False`` if no command was
        matched. The caller is expected to send an "unknown command"
        reply on ``False`` -- the registry stays out of the user-visible
        path so it can be reused by non-DM contexts later.
        """
        if not ctx.tokens:
            return False
        cmd = self._commands.get(ctx.tokens[0])
        if cmd is None:
            return False
        await cmd.handler(ctx)
        return True

    # ------------------------------------------------------------------
    # help formatting
    # ------------------------------------------------------------------

    def format_overview(self) -> str:
        """Top-level ``!help`` text: every registered command's summary.

        Output is stable (sorted by name) so tests can assert on it.
        """
        if not self._commands:
            return "_No commands registered._"
        lines = ["**Admin commands** (DM only)"]
        for name in self.names():
            cmd = self._commands[name]
            lines.append(f"- `{name}` — {cmd.summary}")
        lines.append("")
        lines.append("Send `!help <command>` for detailed usage.")
        return "\n".join(lines)

    def format_command_help(self, name: str) -> str:
        cmd = self._commands.get(name)
        if cmd is None:
            return f"Unknown command `{name}`. Try `!help`."
        # Commands that already begin their `usage` with code-fenced
        # examples are formatted just by adding a header. Plain-text
        # usage strings get their own header too -- the Zulip renderer
        # handles either fine.
        return f"**`{cmd.name}`** — {cmd.summary}\n\n{cmd.usage}"
