"""Admin controls feature for the Zulip bot.

Provides administrative commands for bot operators to view and modify
configuration through direct messages. Only available to Zulip admins/owners.
"""
import logging
from dataclasses import dataclass
from typing import Any, Dict, List

import yaml

from config import ConfigManager
from core.client import ZulipTrioClient
from core.dispatcher import FeatureHandler
from core.models import MessageEvent
from utils.scheduling import DeletionScheduler

logger = logging.getLogger(__name__)


@dataclass
class AdminControlsFeature(FeatureHandler):
    """Admin-only commands for runtime bot configuration.
    
    Attributes:
        client: Zulip client for sending messages
        config_mgr: Configuration manager
        scheduler: Message deletion scheduler
    """
    client: ZulipTrioClient
    config_mgr: ConfigManager
    scheduler: DeletionScheduler

    async def handles(self, event: MessageEvent) -> bool:
        # Only DM messages, starting with "!"
        if event.message_type != "private":
            return False
        if not event.content.strip().startswith("!"):
            return False
        # Check if sender is org admin
        user = await self.client.get_user_by_id(event.sender_id)
        if not user:
            return False
        # Zulip user roles: is_admin or is_owner usually mark admins
        if not (user.get("is_admin") or user.get("is_owner")):
            return False
        return True

    async def handle(self, event: MessageEvent) -> None:
        lines = event.content.strip().splitlines()
        if not lines:
            return
        cmd_line = lines[0].strip()
        body = "\n".join(lines[1:]).strip()

        if cmd_line.startswith("!config"):
            await self._handle_config(cmd_line, body, event)
        elif cmd_line.startswith("!anon"):
            await self._handle_anon(cmd_line, body, event)
        elif cmd_line.startswith("!access"):
            await self._handle_access(cmd_line, body, event)
        elif cmd_line.startswith("!subscribe"):
            await self._handle_subscribe(cmd_line, body, event)
        else:
            await self.client.send_private_message(
                event.sender_id,
                "Unknown admin command. Supported: !config, !anon, !access, !subscribe",
            )

    async def _handle_config(
        self, cmd: str, body: str, event: MessageEvent  # pylint: disable=unused-argument
    ) -> None:
        """Handle !config admin commands.
        
        Args:
            cmd: Command line (e.g., '!config show')
            body: Additional body text if any (reserved for future use)
            event: Message event from admin
        """
        # For now only "show"
        parts = cmd.split()
        if len(parts) == 2 and parts[1] == "show":
            cfg = self.config_mgr.get()
            # Redact nothing except we avoid secrets (none here currently)
            text = yaml.safe_dump(cfg, sort_keys=False)
            await self.client.send_private_message(
                event.sender_id,
                f"Current config:\n```yaml\n{text}\n```",
            )
        else:
            await self.client.send_private_message(
                event.sender_id,
                "Usage: `!config show`",
            )

    async def _handle_anon(
        self, cmd: str, body: str, event: MessageEvent  # pylint: disable=unused-argument
    ) -> None:
        """Handle !anon admin commands.
        
        Commands:
            !anon set stream <name>
            !anon set topic <name>
            !anon set delete_after_minutes <int>
        
        Args:
            cmd: Command line
            body: Additional body text (reserved for future use)
            event: Message event from admin
        """
        parts = cmd.split()
        
        # Handle !anon show
        if len(parts) == 2 and parts[1] == "show":
            cfg = self.config_mgr.get()
            anon_cfg = cfg.get("anonymous_posting", {})
            await self.client.send_private_message(
                event.sender_id,
                f"**Anonymous Posting Configuration:**\n"
                f"• Stream: `{anon_cfg.get('target_stream', 'anonymous')}`\n"
                f"• Topic: `{anon_cfg.get('target_topic', 'general')}`\n"
                f"• Delete after: {anon_cfg.get('delete_after_minutes', 10080)} minutes "
                f"({anon_cfg.get('delete_after_minutes', 10080) // 60 // 24} days)",
            )
            return
        
        if len(parts) != 4 or parts[1] != "set":
            await self.client.send_private_message(
                event.sender_id,
                (
                    "Usage:\n"
                    "`!anon show` - Show current settings\n"
                    "`!anon set stream <name>` - Set target stream\n"
                    "`!anon set topic <name>` - Set target topic\n"
                    "`!anon set delete_after_minutes <int>` - Set deletion delay"
                ),
            )
            return

        field = parts[2]
        value = parts[3]

        cfg = self.config_mgr.get()
        anon_cfg = cfg.setdefault("anonymous_posting", {})

        if field == "stream":
            anon_cfg["target_stream"] = value
        elif field == "topic":
            anon_cfg["target_topic"] = value
        elif field == "delete_after_minutes":
            try:
                anon_cfg["delete_after_minutes"] = int(value)
            except ValueError:
                await self.client.send_private_message(
                    event.sender_id,
                    "delete_after_minutes must be an integer.",
                )
                return
        else:
            await self.client.send_private_message(
                event.sender_id,
                f"Unknown field `{field}`. Allowed: stream, topic, delete_after_minutes.",
            )
            return

        self.config_mgr.update(cfg)
        await self.client.send_private_message(
            event.sender_id,
            f"Anonymous posting config updated: {field}={value}",
        )

    async def _handle_access(self, cmd: str, body: str, event: MessageEvent) -> None:
        """
        !access add
        <yaml body>

        !access remove
        <yaml body>
        """
        parts = cmd.split()
        if len(parts) != 2 or parts[1] not in ("add", "remove"):
            await self.client.send_private_message(
                event.sender_id,
                (
                    "Usage:\n"
                    "!access add\\n"
                    "  stream: access-requests\\n"
                    "  topic: example-topic\\n"
                    "  phrase: \"I want to play a game\"\\n"
                    "  target_stream: game-room\n\n"
                    "!access remove\\n"
                    "  stream: access-requests\\n"
                    "  topic: example-topic\\n"
                    "  phrase: \"I want to play a game\""
                ),
            )
            return

        if not body:
            await self.client.send_private_message(
                event.sender_id,
                "Please provide YAML body for !access add/remove.",
            )
            return

        try:
            data = yaml.safe_load(body)
        except yaml.YAMLError as e:
            await self.client.send_private_message(
                event.sender_id,
                f"Failed to parse YAML: {e}",
            )
            return

        if not isinstance(data, dict):
            await self.client.send_private_message(
                event.sender_id,
                "Body must be a YAML mapping.",
            )
            return

        action = parts[1]
        cfg = self.config_mgr.get()
        p_cfg = cfg.setdefault("private_access", {})
        rules: List[Dict[str, Any]] = p_cfg.setdefault("watch_rules", [])

        if action == "add":
            required_fields = {"stream", "topic", "phrase", "target_stream"}
            if not required_fields.issubset(data.keys()):
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
            await self.client.send_private_message(
                event.sender_id,
                "Access rule added.",
            )
        else:  # remove
            required_fields = {"stream", "topic", "phrase"}
            if not required_fields.issubset(data.keys()):
                await self.client.send_private_message(
                    event.sender_id,
                    "YAML must include: stream, topic, phrase.",
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
            await self.client.send_private_message(
                event.sender_id,
                f"Access rules removed: {removed}.",
            )

    async def _handle_subscribe(
        self, cmd: str, body: str, event: MessageEvent  # pylint: disable=unused-argument
    ) -> None:
        """Handle !subscribe command to subscribe bot to streams.
        
        Commands:
            !subscribe <stream1> [stream2] [stream3] ...
        
        Args:
            cmd: Command line with stream names
            body: Additional body text (reserved for future use)
            event: Message event from admin
        """
        parts = cmd.split()
        if len(parts) < 2:
            await self.client.send_private_message(
                event.sender_id,
                "Usage: `!subscribe <stream1> [stream2] [stream3] ...`\n"
                "Example: `!subscribe general announcements anonymous`",
            )
            return

        streams = parts[1:]  # Everything after !subscribe
        
        # Subscribe the bot to the streams
        result = await self.client.subscribe_bot_to_streams(streams)
        
        if result.get("result") == "success":
            subscribed = result.get("subscribed", {})
            already_subscribed = result.get("already_subscribed", {})
            
            response_parts = []
            if subscribed:
                bot_email = list(subscribed.keys())[0] if subscribed else "bot"
                new_streams = subscribed.get(bot_email, [])
                if new_streams:
                    response_parts.append(
                        f"✅ Subscribed to: {', '.join(new_streams)}"
                    )
            
            if already_subscribed:
                bot_email = list(already_subscribed.keys())[0] if already_subscribed else "bot"
                existing = already_subscribed.get(bot_email, [])
                if existing:
                    response_parts.append(
                        f"ℹ️ Already subscribed to: {', '.join(existing)}"
                    )
            
            if not response_parts:
                response_parts.append("✅ Subscription request completed")
            
            await self.client.send_private_message(
                event.sender_id,
                "\n".join(response_parts),
            )
        else:
            error_msg = result.get("msg", "Unknown error")
            await self.client.send_private_message(
                event.sender_id,
                f"❌ Failed to subscribe: {error_msg}",
            )
