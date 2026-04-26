"""Event dispatching system for routing Zulip messages to feature handlers."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from core.models import MessageEvent, parse_message_event

logger = logging.getLogger(__name__)


# A realm_user.add handler is just `async fn(user_id: int) -> None`.
# Kept narrow: handlers don't get the raw event, so a future change to
# the Zulip event shape only touches the dispatcher.
RealmUserAddHandler = Callable[[int], Awaitable[None]]


class FeatureHandler:
    """Interface for feature modules."""

    async def handles(self, event: MessageEvent) -> bool:
        raise NotImplementedError

    async def handle(self, event: MessageEvent) -> None:
        raise NotImplementedError


class Dispatcher:
    """Routes Zulip events to registered feature / realm-user handlers.

    Drops message events whose sender is the bot itself (`bot_user_id`)
    so handlers cannot self-trigger via the bot's own outgoing messages.
    """

    def __init__(self, bot_user_id: int | None = None) -> None:
        self._features: list[FeatureHandler] = []
        self._realm_user_add_handlers: list[RealmUserAddHandler] = []
        self._bot_user_id = bot_user_id

    def set_bot_user_id(self, bot_user_id: int | None) -> None:
        self._bot_user_id = bot_user_id

    def register_feature(self, feature: FeatureHandler) -> None:
        self._features.append(feature)

    def register_realm_user_add_handler(self, handler: RealmUserAddHandler) -> None:
        """Register a callback for ``realm_user`` op=``add`` events."""
        self._realm_user_add_handlers.append(handler)

    async def dispatch_event(self, event_dict: dict) -> None:
        event_type = event_dict.get("type")
        if event_type == "realm_user":
            await self._dispatch_realm_user(event_dict)
            return
        if event_type != "message":
            return
        await self._dispatch_message(event_dict)

    async def _dispatch_message(self, event_dict: dict) -> None:
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

    async def _dispatch_realm_user(self, event_dict: dict) -> None:
        if event_dict.get("op") != "add":
            return
        person = event_dict.get("person") or {}
        user_id = person.get("user_id")
        if not isinstance(user_id, int):
            return
        # Don't welcome bots (including ourselves on first connect).
        if person.get("is_bot"):
            return
        for handler in self._realm_user_add_handlers:
            try:
                await handler(user_id)
            except Exception:
                logger.exception("Error in realm_user.add handler %s", handler)
