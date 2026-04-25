"""Main entry point for the Zulip bot."""

from __future__ import annotations

import logging
import os
from typing import Any

import trio

from config import ConfigManager
from core.client import QueueInvalidated, ZulipTrioClient
from core.dispatcher import Dispatcher
from features.admin_controls import AdminControlsFeature
from features.anonymous_posting import AnonymousPostingFeature
from features.private_access import PrivateAccessFeature
from utils.scheduling import DeletionScheduler

logger = logging.getLogger(__name__)


def _configure_logging(config_mgr: ConfigManager) -> None:
    level_name = config_mgr.get().get("logging", {}).get("level", "INFO") or "INFO"
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


async def _event_loop(client: ZulipTrioClient, dispatcher: Dispatcher) -> None:
    """Register an event queue and dispatch events forever.

    On `BAD_EVENT_QUEUE_ID` the queue is re-registered automatically.
    """
    while True:
        queue: dict[str, Any] = await client.register(
            event_types=["message"],
            client_gravatar=False,
            apply_markdown=False,
        )
        logger.info(
            "Registered event queue id=%s last_event_id=%s",
            queue.get("queue_id"),
            queue.get("last_event_id"),
        )
        try:
            async for event in client.events(queue):
                await dispatcher.dispatch_event(event)
        except QueueInvalidated:
            logger.info("Re-registering event queue after invalidation")
            continue


async def main() -> None:
    """Initialize and run the Zulip bot."""
    # --- 1. Config + logging ---------------------------------------------
    # Load YAML config first so we can honour `logging.level` from the file
    # before anything else logs. ZULIP_BOT_VC_CONFIG lets ops point at a
    # config outside the working directory (e.g. mounted secret).
    config_path = os.environ.get("ZULIP_BOT_VC_CONFIG", "config.yaml")
    config_mgr = ConfigManager(config_path)
    config_mgr.load()
    _configure_logging(config_mgr)
    logger.info("Starting zulip_bot-vc")

    # --- 2. Zulip client + identity --------------------------------------
    # Reads ~/.zuliprc or $ZULIP_CONFIG_FILE. We immediately fetch our own
    # profile so the dispatcher can drop events authored by us (otherwise
    # the bot can react to its own DMs and feedback-loop).
    client = ZulipTrioClient.from_env_or_rc()

    bot_user = await client.get_own_user()
    if not bot_user:
        # Fail closed: without our identity we cannot self-filter, which
        # could cause feedback loops in feature handlers.
        raise RuntimeError(
            "Could not retrieve bot profile; refusing to start. "
            "Check that the bot's API key has profile-read access."
        )
    bot_user_id = bot_user.get("user_id")
    logger.info(
        "Bot authenticated as: %s (email=%s, user_id=%s)",
        bot_user.get("full_name"),
        bot_user.get("email"),
        bot_user_id,
    )

    # --- 3. Wire features ------------------------------------------------
    # The scheduler is a callable consumer of `client.delete_message`; it
    # doesn't import the client class, which keeps it unit-testable with a
    # plain async function in tests/.
    scheduler = DeletionScheduler(delete_fn=client.delete_message)
    dispatcher = Dispatcher(bot_user_id=bot_user_id if isinstance(bot_user_id, int) else None)

    admin_feature = AdminControlsFeature(client=client, config_mgr=config_mgr, scheduler=scheduler)
    anon_feature = AnonymousPostingFeature(
        client=client, config_mgr=config_mgr, scheduler=scheduler
    )
    access_feature = PrivateAccessFeature(client=client, config_mgr=config_mgr)
    # Order matters: dispatcher walks features in registration order and
    # short-circuits on the first `handles()` that returns True. Admin must
    # be first so `!`-prefixed DMs are routed to admin commands before
    # anonymous posting tries to interpret them as content.
    for f in (admin_feature, anon_feature, access_feature):
        dispatcher.register_feature(f)

    # --- 4. Run the two long-lived tasks under one nursery ---------------
    # If either task crashes, trio cancels the other and propagates the
    # exception out of `trio.run` — i.e. we exit the process rather than
    # silently keep one half alive.
    async with trio.open_nursery() as nursery:
        nursery.start_soon(scheduler.run)
        nursery.start_soon(_event_loop, client, dispatcher)


if __name__ == "__main__":
    trio.run(main)
