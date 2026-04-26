"""Admin DM commands.

Commands are gated on Zulip's ``is_admin`` / ``is_owner`` flags with a
short-lived cache to avoid round-tripping ``get_user_by_id`` per command.
The first whitespace-separated token is matched exactly via
:class:`core.commands.CommandRegistry` (no ``startswith`` collisions).
Argument parsing uses :mod:`shlex` so multi-word stream/topic names
quoted with double quotes work as expected.

Adding a new command:

1. Write an ``async _handle_xxx(ctx: CommandContext)`` method.
2. Register it in :meth:`AdminControlsFeature._build_registry` with a
   :class:`Command` describing its name / summary / usage.
3. ``!help`` and ``!help <name>`` will pick it up automatically.
"""

from __future__ import annotations

import logging
import re
import shlex
import time
from dataclasses import dataclass, field
from typing import Any

import yaml

from config import ConfigManager
from core.client import ClientProtocol
from core.commands import Command, CommandContext, CommandRegistry
from core.dispatcher import FeatureHandler
from core.models import MessageEvent
from utils.scheduling import DeletionScheduler

logger = logging.getLogger(__name__)


# Keys to redact from `!config show` output. Pattern is matched
# case-insensitively against the leaf key name.
_REDACT_PATTERN = re.compile(r"(token|secret|password|api[_-]?key|webhook)", re.IGNORECASE)


def _redact(value: Any) -> Any:
    """Return a deep copy of ``value`` with sensitive leaves replaced."""
    if isinstance(value, dict):
        return {
            k: ("***REDACTED***" if _REDACT_PATTERN.search(k) else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _coerce_bool(value: str) -> bool:
    return value.strip().lower() in {"true", "1", "yes", "on"}


@dataclass
class AdminControlsFeature(FeatureHandler):
    client: ClientProtocol
    config_mgr: ConfigManager
    scheduler: DeletionScheduler
    # role cache: sender_id -> (is_admin_or_owner, expires_at_monotonic)
    _role_cache: dict[int, tuple[bool, float]] = field(default_factory=dict, repr=False)
    _registry: CommandRegistry = field(default_factory=CommandRegistry, repr=False)

    def __post_init__(self) -> None:
        self._build_registry()

    # ---------------------------------------------------------------- guards

    def _admin_cfg(self) -> dict[str, Any]:
        return self.config_mgr.get().get("admin", {})

    async def _is_admin(self, sender_id: int) -> bool:
        ttl = float(self._admin_cfg().get("role_cache_ttl_seconds", 60))
        cached = self._role_cache.get(sender_id)
        if cached is not None:
            ok, expires_at = cached
            if time.monotonic() < expires_at:
                return ok
        user = await self.client.get_user_by_id(sender_id)
        ok = bool(user and (user.get("is_admin") or user.get("is_owner")))
        self._role_cache[sender_id] = (ok, time.monotonic() + ttl)
        return ok

    async def handles(self, event: MessageEvent) -> bool:
        if event.message_type != "private":
            return False
        if not event.content.strip().startswith("!"):
            return False
        return await self._is_admin(event.sender_id)

    # ---------------------------------------------------------------- routing

    async def handle(self, event: MessageEvent) -> None:
        lines = event.content.strip().splitlines()
        if not lines:
            return
        cmd_line = lines[0].strip()
        body = "\n".join(lines[1:]).strip()

        try:
            tokens = shlex.split(cmd_line)
        except ValueError as e:
            await self.client.send_private_message(event.sender_id, f"Could not parse command: {e}")
            return
        if not tokens:
            return

        ctx = CommandContext(event=event, tokens=tokens, body=body)
        dispatched = await self._registry.dispatch(ctx)
        if not dispatched:
            # Listing every registered name in the error keeps the
            # message useful even after new commands are added.
            known = ", ".join(self._registry.names())
            await self.client.send_private_message(
                event.sender_id,
                f"Unknown command `{tokens[0]}`. Try `!help`. Supported: {known}",
            )

    # ---------------------------------------------------------------- registry

    def _build_registry(self) -> None:
        """Register every admin command on this feature.

        Called once from :meth:`__post_init__`. Adding a new command is
        a one-line change here plus the handler method below.
        """
        self._registry.register(
            Command(
                name="!help",
                summary="show this message, or detailed usage for one command",
                usage=(
                    "`!help` — list every admin command.\n"
                    "`!help <command>` — detailed usage for one command, e.g. `!help !anon`."
                ),
                handler=self._handle_help,
            )
        )
        self._registry.register(
            Command(
                name="!config",
                summary="inspect bot configuration (secrets redacted)",
                usage="`!config show` — print the current config as YAML.",
                handler=self._handle_config,
            )
        )
        self._registry.register(
            Command(
                name="!anon",
                summary="manage the anonymous-posting feature",
                usage=(
                    "`!anon show` — current settings.\n"
                    '`!anon set stream "<name>"` — destination stream.\n'
                    '`!anon set topic "<name>"` — destination topic.\n'
                    "`!anon set delete_after_minutes <int>`\n"
                    "`!anon set max_content_length <int>`\n"
                    "`!anon set min_seconds_between_posts <int>`\n"
                    "`!anon set pending_ttl_minutes <int>`\n"
                    "`!anon set enabled true|false`\n"
                    "`!anon set scrub_wildcard_mentions true|false`"
                ),
                handler=self._handle_anon,
            )
        )
        self._registry.register(
            Command(
                name="!access",
                summary="manage private-access watch rules (YAML body)",
                usage=(
                    "Add a rule (the YAML body comes on the lines after the command):\n"
                    "```\n"
                    "!access add\n"
                    "stream: access-requests\n"
                    "topic: example-topic\n"
                    'phrase: "I want to play a game"\n'
                    "target_stream: game-room\n"
                    "```\n"
                    "Remove a rule:\n"
                    "```\n"
                    "!access remove\n"
                    "stream: access-requests\n"
                    "topic: example-topic\n"
                    'phrase: "I want to play a game"\n'
                    "```"
                ),
                handler=self._handle_access,
            )
        )
        self._registry.register(
            Command(
                name="!subscribe",
                summary="subscribe the bot to one or more streams",
                usage=(
                    "`!subscribe <stream> [stream ...]`\n"
                    'Example: `!subscribe general announcements "anon room"`'
                ),
                handler=self._handle_subscribe,
            )
        )

    # ---------------------------------------------------------------- !help

    async def _handle_help(self, ctx: CommandContext) -> None:
        # `!help` -> overview; `!help <name>` -> per-command usage.
        # Tolerate both `!help anon` and `!help !anon` since users
        # remember the leading bang inconsistently.
        if len(ctx.tokens) >= 2:
            name = ctx.tokens[1]
            if not name.startswith("!"):
                name = "!" + name
            text = self._registry.format_command_help(name)
        else:
            text = self._registry.format_overview()
        await self.client.send_private_message(ctx.sender_id, text)

    # ---------------------------------------------------------------- !config

    async def _handle_config(self, ctx: CommandContext) -> None:
        if len(ctx.tokens) == 2 and ctx.tokens[1] == "show":
            cfg = self.config_mgr.get()
            text = yaml.safe_dump(_redact(cfg), sort_keys=False)
            await self.client.send_private_message(
                ctx.sender_id, f"Current config:\n```yaml\n{text}\n```"
            )
            return
        await self._send_usage(ctx, "!config")

    # ---------------------------------------------------------------- !anon

    async def _handle_anon(self, ctx: CommandContext) -> None:
        tokens = ctx.tokens
        if len(tokens) == 2 and tokens[1] == "show":
            anon_cfg = self.config_mgr.get().get("anonymous_posting", {})
            await self.client.send_private_message(
                ctx.sender_id,
                "**Anonymous Posting Configuration:**\n"
                f"- Enabled: `{anon_cfg.get('enabled', False)}`\n"
                f"- Stream: `{anon_cfg.get('target_stream', 'anonymous')}`\n"
                f"- Topic: `{anon_cfg.get('target_topic', 'general')}`\n"
                f"- Delete after: "
                f"{anon_cfg.get('delete_after_minutes', 10080)} minutes "
                f"({anon_cfg.get('delete_after_minutes', 10080) // 60 // 24} days)\n"
                f"- Max content length: "
                f"{anon_cfg.get('max_content_length', 4000)}\n"
                f"- Cooldown (sec): "
                f"{anon_cfg.get('min_seconds_between_posts', 30)}\n"
                f"- Scrub wildcard mentions: "
                f"`{anon_cfg.get('scrub_wildcard_mentions', True)}`",
            )
            return

        if len(tokens) != 4 or tokens[1] != "set":
            await self._send_usage(ctx, "!anon")
            return

        target_field = tokens[2]
        value: Any = tokens[3]

        cfg = self.config_mgr.get()
        anon_cfg = cfg.setdefault("anonymous_posting", {})

        if target_field in {"stream", "target_stream"}:
            anon_cfg["target_stream"] = value
        elif target_field in {"topic", "target_topic"}:
            anon_cfg["target_topic"] = value
        elif target_field in {
            "delete_after_minutes",
            "max_content_length",
            "min_seconds_between_posts",
            "pending_ttl_minutes",
        }:
            try:
                anon_cfg[target_field] = int(value)
            except ValueError:
                await self.client.send_private_message(
                    ctx.sender_id, f"{target_field} must be an integer."
                )
                return
        elif target_field == "enabled":
            anon_cfg["enabled"] = _coerce_bool(str(value))
        elif target_field == "scrub_wildcard_mentions":
            anon_cfg["scrub_wildcard_mentions"] = _coerce_bool(str(value))
        else:
            await self.client.send_private_message(
                ctx.sender_id,
                f"Unknown field `{target_field}`. See `!help !anon` for valid fields.",
            )
            return

        self.config_mgr.update(cfg)
        await self.client.send_private_message(
            ctx.sender_id,
            f"Anonymous posting config updated: {target_field}={value}",
        )

    # ---------------------------------------------------------------- !access

    async def _handle_access(self, ctx: CommandContext) -> None:
        tokens = ctx.tokens
        body = ctx.body
        if len(tokens) != 2 or tokens[1] not in ("add", "remove"):
            await self._send_usage(ctx, "!access")
            return

        if not body:
            await self.client.send_private_message(
                ctx.sender_id, "Please provide a YAML body for !access."
            )
            return

        try:
            data = yaml.safe_load(body)
        except yaml.YAMLError as e:
            await self.client.send_private_message(ctx.sender_id, f"Failed to parse YAML: {e}")
            return

        if not isinstance(data, dict):
            await self.client.send_private_message(ctx.sender_id, "Body must be a YAML mapping.")
            return

        action = tokens[1]
        cfg = self.config_mgr.get()
        p_cfg = cfg.setdefault("private_access", {})
        rules: list[dict[str, Any]] = p_cfg.setdefault("watch_rules", [])

        if action == "add":
            required = {"stream", "topic", "phrase", "target_stream"}
            if not required.issubset(data.keys()):
                await self.client.send_private_message(
                    ctx.sender_id,
                    "YAML must include: stream, topic, phrase, target_stream.",
                )
                return
            rules.append(
                {
                    "stream": data["stream"],
                    "topic": data["topic"],
                    "phrase": data["phrase"],
                    "target_stream": data["target_stream"],
                }
            )
            self.config_mgr.update(cfg)
            await self.client.send_private_message(ctx.sender_id, "Access rule added.")
            return

        # remove
        required = {"stream", "topic", "phrase"}
        if not required.issubset(data.keys()):
            await self.client.send_private_message(
                ctx.sender_id, "YAML must include: stream, topic, phrase."
            )
            return
        before = len(rules)
        rules = [
            r
            for r in rules
            if not (
                r.get("stream") == data["stream"]
                and r.get("topic") == data["topic"]
                and r.get("phrase") == data["phrase"]
            )
        ]
        p_cfg["watch_rules"] = rules
        self.config_mgr.update(cfg)
        removed = before - len(rules)
        await self.client.send_private_message(ctx.sender_id, f"Access rules removed: {removed}.")

    # ---------------------------------------------------------------- !subscribe

    async def _handle_subscribe(self, ctx: CommandContext) -> None:
        tokens = ctx.tokens
        if len(tokens) < 2:
            await self._send_usage(ctx, "!subscribe")
            return
        streams = tokens[1:]
        result = await self.client.subscribe_bot_to_streams(streams)

        if result.get("result") != "success":
            await self.client.send_private_message(
                ctx.sender_id,
                f"❌ Failed to subscribe: {result.get('msg', 'unknown error')}",
            )
            return

        subscribed = result.get("subscribed") or {}
        already = result.get("already_subscribed") or {}
        new_streams: list[str] = []
        for v in subscribed.values():
            if isinstance(v, list):
                new_streams.extend(v)
        existing: list[str] = []
        for v in already.values():
            if isinstance(v, list):
                existing.extend(v)

        parts: list[str] = []
        if new_streams:
            parts.append(f"✅ Subscribed to: {', '.join(new_streams)}")
        if existing:
            parts.append(f"ℹ️ Already subscribed to: {', '.join(existing)}")
        if not parts:
            parts.append("✅ Subscription request completed")
        await self.client.send_private_message(ctx.sender_id, "\n".join(parts))

    # ---------------------------------------------------------------- helpers

    async def _send_usage(self, ctx: CommandContext, name: str) -> None:
        """Reply with the registry-rendered help for *name*.

        Centralising this means every "wrong arguments" path lands the
        same well-formatted text without each handler re-typing it.
        """
        await self.client.send_private_message(
            ctx.sender_id, self._registry.format_command_help(name)
        )
