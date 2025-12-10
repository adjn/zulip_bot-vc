import json
import logging
import os
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional

import trio
import zulip

logger = logging.getLogger(__name__)


class ZulipTrioClient:
    """
    Trio-friendly wrapper around zulip.Client.
    Uses trio.to_thread.run_sync for blocking calls.
    """

    def __init__(self, client: zulip.Client) -> None:
        self._client = client

    @classmethod
    def from_env_or_rc(cls) -> "ZulipTrioClient":
        config_file = os.environ.get("ZULIP_CONFIG_FILE")  # optional override
        if config_file:
            client = zulip.Client(config_file=config_file)
        else:
            # This looks for ~/.zuliprc or equivalent env vars.
            client = zulip.Client()
        return cls(client)

    async def register(self, **kwargs: Any) -> Dict[str, Any]:
        def _register() -> Dict[str, Any]:
            return self._client.register(**kwargs)

        return await trio.to_thread.run_sync(_register)

    async def events(self, queue: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        """
        Async generator yielding events from Zulip.
        """
        queue_id = queue["queue_id"]
        last_event_id = queue["last_event_id"]

        while True:
            def _get_events() -> Dict[str, Any]:
                return self._client.get_events(
                    queue_id=queue_id,
                    last_event_id=last_event_id,
                    dont_block=False,
                    timeout=90,
                )

            res = await trio.to_thread.run_sync(_get_events)
            if res.get("result") != "success":
                logger.warning("Error from get_events: %s", json.dumps(res))
                await trio.sleep(1)
                continue

            events = res.get("events", [])
            for event in events:
                last_event_id = max(last_event_id, event.get("id", last_event_id))
                yield event

    async def send_private_message(self, to_user_id: int, content: str) -> Optional[int]:
        def _send() -> Dict[str, Any]:
            return self._client.send_message(
                {
                    "type": "private",
                    "to": [to_user_id],
                    "content": content,
                }
            )

        res = await trio.to_thread.run_sync(_send)
        if res.get("result") == "success":
            return res.get("id")
        logger.warning("Failed to send private message: %s", res)
        return None

    async def send_stream_message(
        self, stream: str, topic: str, content: str
    ) -> Optional[int]:
        def _send() -> Dict[str, Any]:
            return self._client.send_message(
                {
                    "type": "stream",
                    "to": stream,
                    "topic": topic,
                    "content": content,
                }
            )

        res = await trio.to_thread.run_sync(_send)
        if res.get("result") == "success":
            return res.get("id")
        logger.warning("Failed to send stream message: %s", res)
        return None

    async def react_to_message(self, message_id: int, emoji_name: str) -> None:
        def _react() -> Dict[str, Any]:
            return self._client.add_reaction(
                {
                    "message_id": message_id,
                    "emoji_name": emoji_name,
                }
            )

        res = await trio.to_thread.run_sync(_react)
        if res.get("result") != "success":
            logger.warning("Failed to add reaction: %s", res)

    async def add_user_subscriptions(
        self, user_id: int, streams: Iterable[str]
    ) -> None:
        """Subscribe a user to one or more streams.
        
        Args:
            user_id: ID of the user to subscribe
            streams: Stream names to subscribe to
        """
        stream_names = list(streams)
        if not stream_names:
            return

        def _subscribe() -> Dict[str, Any]:
            return self._client.add_subscriptions(
                streams=[{"name": s} for s in stream_names],
                principals=[user_id],
            )

        res = await trio.to_thread.run_sync(_subscribe)
        if res.get("result") != "success":
            logger.warning("Failed to subscribe user %s to %s: %s", user_id, stream_names, res)

    async def subscribe_bot_to_streams(self, streams: Iterable[str]) -> Dict[str, Any]:
        """Subscribe the bot itself to one or more streams.
        
        Args:
            streams: Stream names to subscribe to
            
        Returns:
            Result dictionary from Zulip API
        """
        stream_names = list(streams)
        if not stream_names:
            return {"result": "error", "msg": "No streams provided"}

        def _subscribe() -> Dict[str, Any]:
            return self._client.add_subscriptions(
                streams=[{"name": s} for s in stream_names],
            )

        res = await trio.to_thread.run_sync(_subscribe)
        return res

    async def delete_message(self, message_id: int) -> bool:
        def _delete() -> Dict[str, Any]:
            return self._client.delete_message(message_id)

        res = await trio.to_thread.run_sync(_delete)
        if res.get("result") == "success":
            return True
        logger.warning("Failed to delete message_id=%s: %s", message_id, res)
        return False

    async def get_user_by_id(self, user_id: int) -> Optional[Dict[str, Any]]:
        def _get() -> Dict[str, Any]:
            return self._client.get_user_by_id(user_id)

        res = await trio.to_thread.run_sync(_get)
        if res.get("result") != "success":
            logger.warning("Failed to get user by id %s: %s", user_id, res)
            return None
        return res.get("user")

    async def get_own_user(self) -> Optional[Dict[str, Any]]:
        def _get() -> Dict[str, Any]:
            return self._client.get_profile()

        res = await trio.to_thread.run_sync(_get)
        if res.get("result") != "success":
            logger.warning("Failed to get own profile: %s", res)
            return None
        return res

    async def list_users(self) -> List[Dict[str, Any]]:
        def _get() -> Dict[str, Any]:
            return self._client.get_users()

        res = await trio.to_thread.run_sync(_get)
        if res.get("result") != "success":
            logger.warning("Failed to list users: %s", res)
            return []
        return res.get("members", [])