"""Main entry point for the Zulip bot."""

from __future__ import annotations

import logging
import os
from typing import Any

import trio

from config import ConfigManager
from core.authz import Authorizer
from core.client import QueueInvalidated, ZulipTrioClient
from core.context import FeatureContext
from core.dispatcher import Dispatcher
from features.admin_controls import AdminControlsFeature
from features.anonymous_posting import AnonymousPostingFeature
from features.private_access import PrivateAccessFeature
from storage.db import Storage
from utils.scheduling import DeletionScheduler

logger = logging.getLogger(__name__)


# Event-loop reliability tunables. Kept as module-level constants so tests
# can patch them low and not actually sleep for a minute.
_REGISTER_BACKOFF_BASE = 1.0
_REGISTER_BACKOFF_CAP = 60.0
# Minimum wall-clock time between two register() calls. Prevents a
# pathological "register, immediate BAD_EVENT_QUEUE_ID, register again"
# busy loop when the server is unhealthy.
_MIN_REREGISTER_INTERVAL = 1.0


def _configure_logging(config_mgr: ConfigManager) -> None:
    level_name = config_mgr.get().get("logging", {}).get("level", "INFO") or "INFO"
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


async def _event_loop(client: ZulipTrioClient, dispatcher: Dispatcher) -> None:
    """Register an event queue and dispatch events forever.

    Recovery model:

    * On ``BAD_EVENT_QUEUE_ID`` the queue is re-registered. To prevent a
      busy-loop when the server is rapidly invalidating queues, we
      enforce ``_MIN_REREGISTER_INTERVAL`` between successive
      registrations.
    * If :meth:`ZulipTrioClient.register` itself fails (transient network
      error, server 5xx), we sleep with exponential backoff up to
      ``_REGISTER_BACKOFF_CAP`` and retry. The backoff counter resets
      after a successful register.
    """
    register_failures = 0
    last_register_time = 0.0
    while True:
        # Rate-limit re-registrations so a flapping server can't spin
        # us into a register-storm.
        if last_register_time > 0:
            elapsed = trio.current_time() - last_register_time
            if elapsed < _MIN_REREGISTER_INTERVAL:
                await trio.sleep(_MIN_REREGISTER_INTERVAL - elapsed)

        try:
            queue: dict[str, Any] = await client.register(
                event_types=["message"],
                client_gravatar=False,
                apply_markdown=False,
            )
        except (RuntimeError, OSError, ConnectionError, TimeoutError) as exc:
            register_failures += 1
            sleep_for = min(
                _REGISTER_BACKOFF_BASE * (2 ** (register_failures - 1)),
                _REGISTER_BACKOFF_CAP,
            )
            logger.warning(
                "register() failed (%s); retrying in %.1fs (failure #%d)",
                exc,
                sleep_for,
                register_failures,
            )
            await trio.sleep(sleep_for)
            continue

        register_failures = 0
        last_register_time = trio.current_time()
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
    # Open the durable storage before constructing the scheduler/feature
    # so any pre-existing scheduled deletions are picked up on the next
    # scheduler tick. BOT_DB_PATH overrides config for ops convenience
    # (e.g. tests, ephemeral containers, alternative volumes).
    db_path = os.environ.get(
        "BOT_DB_PATH",
        config_mgr.get().get("storage", {}).get("db_path", "./data/bot.db"),
    )
    try:
        storage = await Storage.open(db_path)
    except OSError as e:
        # Fail closed: refusing to start is safer than silently reverting
        # to in-memory mode and quietly breaking the privacy contract.
        raise RuntimeError(
            f"Could not open storage at {db_path!r}: {e}. "
            "Set BOT_DB_PATH to a writable location or fix permissions."
        ) from e
    logger.info("Storage opened at %s", db_path)

    # The scheduler is a callable consumer of `client.delete_message`; it
    # doesn't import the client class, which keeps it unit-testable with a
    # plain async function in tests/.
    scheduler = DeletionScheduler(delete_fn=client.delete_message, storage=storage)
    dispatcher = Dispatcher(bot_user_id=bot_user_id if isinstance(bot_user_id, int) else None)
    authz = Authorizer(client=client, config_mgr=config_mgr)

    # Single shared dependency container; new cross-cutting resources
    # (audit log, authz, …) plug in by adding a field to FeatureContext
    # rather than touching every feature constructor.
    ctx = FeatureContext(
        client=client,
        config_mgr=config_mgr,
        storage=storage,
        scheduler=scheduler,
        authz=authz,
        bot_user_id=bot_user_id if isinstance(bot_user_id, int) else None,
    )

    admin_feature = AdminControlsFeature(ctx=ctx)
    anon_feature = AnonymousPostingFeature(ctx=ctx)
    access_feature = PrivateAccessFeature(ctx=ctx)
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
    try:
        async with trio.open_nursery() as nursery:
            nursery.start_soon(scheduler.run)
            nursery.start_soon(_event_loop, client, dispatcher)
    finally:
        await storage.close()


if __name__ == "__main__":
    trio.run(main)
