"""Admin DM commands.

Commands are gated on Zulip's ``is_admin`` / ``is_owner`` flags with a
short-lived cache to avoid round-tripping ``get_user_by_id`` per command.
The first whitespace-separated token is matched exactly (no
``startswith`` collisions). Argument parsing uses :mod:`shlex` so
multi-word stream/topic names quoted with double quotes work as expected.

Subcommands:

* ``!config show``
* ``!anon show`` / ``!anon set <field> <value>``
* ``!access add`` / ``!access remove`` (YAML body on subsequent lines)
* ``!subscribe <stream> [stream ...]``
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


@dataclass
class AdminControlsFeature(FeatureHandler):
    client: ClientProtocol
    config_mgr: ConfigManager
    scheduler: DeletionScheduler
    # role cache: sender_id -> (is_admin_or_owner, expires_at_monotonic)
    _role_cache: dict[int, tuple[bool, float]] = field(default_factory=dict, repr=False)

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
        head = tokens[0]

        if head == "!config":
            await self._handle_config(tokens, body, event)
        elif head == "!anon":
            await self._handle_anon(tokens, body, event)
        elif head == "!access":
            await self._handle_access(tokens, body, event)
        elif head == "!subscribe":
            await self._handle_subscribe(tokens, body, event)
        elif head == "!help":
            await self._handle_help(event)
        else:
            await self.client.send_private_message(
                event.sender_id,
                "Unknown command. Try `!help`. "
                "Supported: !config, !anon, !access, !subscribe, !help",
            )

    # ---------------------------------------------------------------- !help

    async def _handle_help(self, event: MessageEvent) -> None:
        await self.client.send_private_message(
            event.sender_id,
            (
                "**Admin commands** (DM only)\n"
                "- `!config show` — show current config (with secrets redacted)\n"
                "- `!anon show` / `!anon set <field> <value>` — anonymous posting settings\n"
                "- `!access add` / `!access remove` — manage private-access rules (YAML body)\n"
                "- `!subscribe <stream> [stream ...]` — subscribe the bot to streams\n"
                "- `!help` — this message"
            ),
        )

    # ---------------------------------------------------------------- !config

    async def _handle_config(self, tokens: list[str], _body: str, event: MessageEvent) -> None:
        if len(tokens) == 2 and tokens[1] == "show":
            cfg = self.config_mgr.get()
            text = yaml.safe_dump(_redact(cfg), sort_keys=False)
            await self.client.send_private_message(
                event.sender_id, f"Current config:\n```yaml\n{text}\n```"
            )
            return
        await self.client.send_private_message(event.sender_id, "Usage: `!config show`")

    # ---------------------------------------------------------------- !anon

    async def _handle_anon(self, tokens: list[str], _body: str, event: MessageEvent) -> None:
        if len(tokens) == 2 and tokens[1] == "show":
            anon_cfg = self.config_mgr.get().get("anonymous_posting", {})
            await self.client.send_private_message(
                event.sender_id,
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
            await self.client.send_private_message(
                event.sender_id,
                "Usage:\n"
                "`!anon show`\n"
                '`!anon set stream "<name>"`\n'
                '`!anon set topic "<name>"`\n'
                "`!anon set delete_after_minutes <int>`\n"
                "`!anon set max_content_length <int>`\n"
                "`!anon set min_seconds_between_posts <int>`\n"
                "`!anon set enabled true|false`",
            )
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
                    event.sender_id, f"{target_field} must be an integer."
                )
                return
        elif target_field == "enabled":
            anon_cfg["enabled"] = str(value).strip().lower() in {"true", "1", "yes", "on"}
        elif target_field == "scrub_wildcard_mentions":
            anon_cfg["scrub_wildcard_mentions"] = str(value).strip().lower() in {
                "true",
                "1",
                "yes",
                "on",
            }
        else:
            await self.client.send_private_message(
                event.sender_id,
                f"Unknown field `{target_field}`.",
            )
            return

        self.config_mgr.update(cfg)
        await self.client.send_private_message(
            event.sender_id,
            f"Anonymous posting config updated: {target_field}={value}",
        )

    # ---------------------------------------------------------------- !access

    async def _handle_access(self, tokens: list[str], body: str, event: MessageEvent) -> None:
        if len(tokens) != 2 or tokens[1] not in ("add", "remove"):
            await self.client.send_private_message(
                event.sender_id,
                "Usage:\n"
                "```\n"
                "!access add\n"
                "stream: access-requests\n"
                "topic: example-topic\n"
                'phrase: "I want to play a game"\n'
                "target_stream: game-room\n"
                "```\n"
                "or\n"
                "```\n"
                "!access remove\n"
                "stream: access-requests\n"
                "topic: example-topic\n"
                'phrase: "I want to play a game"\n'
                "```",
            )
            return

        if not body:
            await self.client.send_private_message(
                event.sender_id, "Please provide a YAML body for !access."
            )
            return

        try:
            data = yaml.safe_load(body)
        except yaml.YAMLError as e:
            await self.client.send_private_message(event.sender_id, f"Failed to parse YAML: {e}")
            return

        if not isinstance(data, dict):
            await self.client.send_private_message(event.sender_id, "Body must be a YAML mapping.")
            return

        action = tokens[1]
        cfg = self.config_mgr.get()
        p_cfg = cfg.setdefault("private_access", {})
        rules: list[dict[str, Any]] = p_cfg.setdefault("watch_rules", [])

        if action == "add":
            required = {"stream", "topic", "phrase", "target_stream"}
            if not required.issubset(data.keys()):
                await self.client.send_private_message(
                    event.sender_id,
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
            await self.client.send_private_message(event.sender_id, "Access rule added.")
            return

        # remove
        required = {"stream", "topic", "phrase"}
        if not required.issubset(data.keys()):
            await self.client.send_private_message(
                event.sender_id, "YAML must include: stream, topic, phrase."
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
        await self.client.send_private_message(event.sender_id, f"Access rules removed: {removed}.")

    # ---------------------------------------------------------------- !subscribe

    async def _handle_subscribe(self, tokens: list[str], _body: str, event: MessageEvent) -> None:
        if len(tokens) < 2:
            await self.client.send_private_message(
                event.sender_id,
                "Usage: `!subscribe <stream> [stream ...]`\n"
                'Example: `!subscribe general announcements "anon room"`',
            )
            return
        streams = tokens[1:]
        result = await self.client.subscribe_bot_to_streams(streams)

        if result.get("result") != "success":
            await self.client.send_private_message(
                event.sender_id,
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
        await self.client.send_private_message(event.sender_id, "\n".join(parts))
