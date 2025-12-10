"""Anonymous posting feature for the Zulip bot.

Allows users to send anonymous messages via DM with a confirmation workflow.
Messages are posted to a configured stream/topic and optionally deleted after
a specified time period.
"""
import logging
from dataclasses import dataclass
from typing import Dict

from config import ConfigManager
from core.client import ZulipTrioClient
from core.dispatcher import FeatureHandler
from core.models import MessageEvent
from utils.scheduling import DeletionScheduler

logger = logging.getLogger(__name__)


@dataclass
class PendingAnon:
    """Tracks a pending anonymous post awaiting user confirmation.
    
    Attributes:
        original_message_id: ID of the original DM message
        original_content: Content of the message to post anonymously
    """
    original_message_id: int
    original_content: str


class AnonymousPostingFeature(FeatureHandler):
    """
    Handles anonymous posting via DM with confirmation and scheduled deletion.
    """

    def __init__(
        self,
        client: ZulipTrioClient,
        config_mgr: ConfigManager,
        scheduler: DeletionScheduler,
    ) -> None:
        self.client = client
        self.config_mgr = config_mgr
        self.scheduler = scheduler
        # key: sender_id, value: PendingAnon
        self._pending: Dict[int, PendingAnon] = {}

    async def handles(self, event: MessageEvent) -> bool:
        cfg = self.config_mgr.get().get("anonymous_posting", {})
        if not cfg.get("enabled", False):
            return False

        # Only DM messages to the bot (type=private)
        return event.message_type == "private"

    async def handle(self, event: MessageEvent) -> None:
        cfg = self.config_mgr.get().get("anonymous_posting", {})
        target_stream = cfg.get("target_stream", "anonymous")
        target_topic = cfg.get("target_topic", "general")
        delete_after_minutes = cfg.get("delete_after_minutes", 7 * 24 * 60)

        normalized = event.content.strip().lower()

        # If user is responding with SEND/CANCEL
        if event.sender_id in self._pending:
            pending = self._pending.pop(event.sender_id)

            if normalized == "send":
                # Post anonymously
                content = f"Anonymous message:\n\n{pending.original_content}"
                anon_msg_id = await self.client.send_stream_message(
                    target_stream, target_topic, content
                )
                if anon_msg_id is not None:
                    # schedule deletion of the anonymous post
                    self.scheduler.schedule_deletion(
                        message_id=anon_msg_id,
                        delete_after_minutes=delete_after_minutes,
                    )
                # Try to delete original DM and control DMs if allowed
                self.scheduler.schedule_deletion(
                    message_id=pending.original_message_id,
                    delete_after_minutes=1,  # soon
                )
                self.scheduler.schedule_deletion(
                    message_id=event.id,
                    delete_after_minutes=1,
                )
                await self.client.send_private_message(
                    event.sender_id,
                    "Your message has been posted anonymously.",
                )
                return

            if normalized == "cancel":
                self.scheduler.schedule_deletion(
                    message_id=pending.original_message_id,
                    delete_after_minutes=1,
                )
                self.scheduler.schedule_deletion(
                    message_id=event.id,
                    delete_after_minutes=1,
                )
                await self.client.send_private_message(
                    event.sender_id,
                    "Okay, your message was not posted.",
                )
                return

            # Unknown input: reset flow and ask user to start over
            await self.client.send_private_message(
                event.sender_id,
                "Unknown input. Please start over by sending your message again.",
            )
            return

        # New DM -> start confirmation flow
        # Save pending confirmation (in memory only, ephemeral)
        self._pending[event.sender_id] = PendingAnon(
            original_message_id=event.id,
            original_content=event.content,
        )

        preview = event.content.strip()
        if len(preview) > 500:
            preview = preview[:500] + " ..."

        await self.client.send_private_message(
            event.sender_id,
            (
                "You wrote:\n\n"
                f"```text\n{preview}\n```\n\n"
                "Reply with `SEND` to post anonymously, or `CANCEL` to discard."
            ),
        )
