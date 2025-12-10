import logging
import os

import trio

from config import ConfigManager
from core.client import ZulipTrioClient
from core.dispatcher import Dispatcher
from features.anonymous_posting import AnonymousPostingFeature
from features.private_access import PrivateAccessFeature
from features.admin_controls import AdminControlsFeature
from utils.scheduling import DeletionScheduler


logger = logging.getLogger(__name__)


async def main() -> None:
    # Basic logging setup
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("Starting zulip_bot-vc")

    # Load config
    config_path = os.environ.get("ZULIP_BOT_VC_CONFIG", "config.yaml")
    config_mgr = ConfigManager(config_path)
    config = config_mgr.load()

    # Create Zulip client (trio wrapper)
    client = ZulipTrioClient.from_env_or_rc()

    # Scheduler for message deletions
    scheduler = DeletionScheduler(client=client)

    # Dispatcher and features
    dispatcher = Dispatcher()

    # Features
    anon_feature = AnonymousPostingFeature(
        client=client,
        config_mgr=config_mgr,
        scheduler=scheduler,
    )
    access_feature = PrivateAccessFeature(
        client=client,
        config_mgr=config_mgr,
    )
    admin_feature = AdminControlsFeature(
        client=client,
        config_mgr=config_mgr,
        scheduler=scheduler,
    )

    features = [admin_feature, anon_feature, access_feature]  # admin first
    for f in features:
        dispatcher.register_feature(f)

    async with trio.open_nursery() as nursery:
        # Start scheduler loop
        nursery.start_soon(scheduler.run)

        # Start event loop
        async def event_loop() -> None:
            # Register an event queue for message events
            queue = await client.register(
                event_types=["message"],
                client_gravatar=False,
                apply_markdown=False,
            )
            logger.info("Registered event queue id=%s", queue.get("queue_id"))

            async for event in client.events(queue):
                await dispatcher.dispatch_event(event)

        nursery.start_soon(event_loop)


if __name__ == "__main__":
    trio.run(main)