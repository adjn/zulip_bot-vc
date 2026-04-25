"""Event dispatching system for routing Zulip messages to feature handlers."""

from __future__ import annotations

import logging

from core.models import MessageEvent, parse_message_event

logger = logging.getLogger(__name__)


class FeatureHandler:
    """Interface for feature modules."""

    async def handles(self, event: MessageEvent) -> bool:
        raise NotImplementedError

    async def handle(self, event: MessageEvent) -> None:
        raise NotImplementedError


class Dispatcher:
    """Routes Zulip message events to registered feature handlers.

    Drops events whose sender is the bot itself (`bot_user_id`) so handlers
    cannot self-trigger via the bot's own outgoing messages.
    """

    def __init__(self, bot_user_id: int | None = None) -> None:
        self._features: list[FeatureHandler] = []
        self._bot_user_id = bot_user_id

    def set_bot_user_id(self, bot_user_id: int | None) -> None:
        self._bot_user_id = bot_user_id

    def register_feature(self, feature: FeatureHandler) -> None:
        self._features.append(feature)

    async def dispatch_event(self, event_dict: dict) -> None:
        msg_event = parse_message_event(event_dict)
        if msg_event is None:
            # Not a message event we care about (presence, typing, …).
            return

        # Self-trigger guard. Without this, the bot's own outgoing messages
        # come back through the event queue and can be re-interpreted as
        # user input — e.g. the anonymous-posting confirmation "You wrote:"
        # could itself look like a fresh anonymous-post submission. Drop
        # them before any feature sees them.
        if self._bot_user_id is not None and msg_event.sender_id == self._bot_user_id:
            return

        # Walk features in registration order. Each feature decides via
        # `handles()` whether it owns the event; we catch and log any
        # exception so one buggy feature can't crash the whole loop.
        for feature in self._features:
            try:
                if await feature.handles(msg_event):
                    await feature.handle(msg_event)
            except Exception:
                logger.exception("Error in feature %s", feature.__class__.__name__)
