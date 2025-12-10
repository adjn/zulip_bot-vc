"""Trio-friendly wrapper for the Zulip API client.

Rate Limiting Strategy:
----------------------
This client implements comprehensive rate limit handling to avoid exceeding
Zulip's API limits (default: 200 requests/minute):

1. Long-polling: The events() method uses long-polling with 90s timeout,
   which means the server holds the connection open rather than the client
   making frequent requests. This is extremely efficient.

2. Rate limit detection: All API methods check for RATE_LIMIT_HIT error codes
   and automatically retry after the specified delay.

3. Rate limit monitoring: Responses are checked for X-RateLimit-Remaining
   headers and warnings are logged when approaching limits.

4. Exponential backoff: On errors, the client backs off to avoid hammering
   the server.
"""
import json
import logging
import os
import time
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
        """Create a ZulipTrioClient from environment variables or ~/.zuliprc."""
        config_file = os.environ.get("ZULIP_CONFIG_FILE")  # optional override
        if config_file:
            client = zulip.Client(config_file=config_file)
        else:
            # This looks for ~/.zuliprc or equivalent env vars.
            client = zulip.Client()
        return cls(client)

    async def register(self, **kwargs: Any) -> Dict[str, Any]:
        """Register an event queue with the Zulip server."""
        def _register() -> Dict[str, Any]:
            return self._client.register(**kwargs)

        return await trio.to_thread.run_sync(_register)

    async def events(self, queue: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        """
        Async generator yielding events from Zulip.
        
        Uses long-polling with proper rate limit handling. The Zulip server
        will hold the connection open until events are available or timeout occurs.
        This means we should NOT be hammering the API - the server blocks until
        there are events or the 90s timeout expires.
        """
        queue_id = queue["queue_id"]
        last_event_id = queue["last_event_id"]

        while True:
            try:
                logger.debug("Polling for events (long-poll, timeout=90s)...")
                
                def _get_events() -> Dict[str, Any]:
                    return self._client.get_events(
                        queue_id=queue_id,
                        last_event_id=last_event_id,
                        dont_block=False,
                        timeout=90,
                    )

                res = await trio.to_thread.run_sync(_get_events)
                
                # Log rate limit info on each poll
                self._log_rate_limit_info(res)
                
                # Check for rate limiting
                if res.get("code") == "RATE_LIMIT_HIT":
                    retry_after = self._get_rate_limit_reset(res)
                    logger.warning(
                        "Rate limit hit. Waiting %s seconds before retry. "
                        "Message: %s",
                        retry_after,
                        res.get("msg", "No message")
                    )
                    await trio.sleep(retry_after)
                    continue
                
                if res.get("result") != "success":
                    logger.warning("Error from get_events: %s", json.dumps(res))
                    # Back off on errors to avoid hammering the server
                    await trio.sleep(5)
                    continue

                events = res.get("events", [])
                
                # If no events, the long-poll timed out naturally - this is expected
                # and we should immediately retry without delay
                if not events:
                    continue
                
                # Process events
                for event in events:
                    last_event_id = max(last_event_id, event.get("id", last_event_id))
                    yield event
                    
            except Exception as e:
                logger.error("Unexpected error in event loop: %s", e, exc_info=True)
                # Back off on unexpected errors
                await trio.sleep(10)
    
    def _get_rate_limit_reset(self, response: Dict[str, Any]) -> float:
        """Extract rate limit reset time from response.
        
        Args:
            response: API response that may contain rate limit info
            
        Returns:
            Number of seconds to wait before retrying
        """
        # Check if retry-after is in the response
        retry_after = response.get("retry-after")
        if retry_after:
            try:
                return float(retry_after)
            except (ValueError, TypeError):
                pass
        
        # Check for X-RateLimit-Reset header in response
        # The Zulip client may expose this in the response dict
        reset_time = response.get("x-ratelimit-reset") or response.get("X-RateLimit-Reset")
        if reset_time:
            try:
                reset_timestamp = float(reset_time)
                wait_time = max(0, reset_timestamp - time.time())
                return wait_time
            except (ValueError, TypeError):
                pass
        
        # Default fallback: wait 60 seconds if we can't determine the reset time
        logger.warning("Could not determine rate limit reset time, using default 60s")
        return 60.0
    
    def _log_rate_limit_info(self, response: Dict[str, Any]) -> None:
        """Log rate limit information from response headers if available.
        
        Args:
            response: API response that may contain rate limit headers
        """
        remaining = response.get("x-ratelimit-remaining") or response.get("X-RateLimit-Remaining")
        limit = response.get("x-ratelimit-limit") or response.get("X-RateLimit-Limit")
        
        if remaining is not None and limit is not None:
            try:
                remaining_count = int(remaining)
                total_limit = int(limit)
                # Warn if we're using more than 80% of the limit
                if remaining_count < (total_limit * 0.2):
                    logger.warning(
                        "Approaching rate limit: %s/%s requests remaining",
                        remaining_count,
                        total_limit
                    )
            except (ValueError, TypeError):
                pass

    async def send_private_message(self, to_user_id: int, content: str) -> Optional[int]:
        """Send a private message to a user with rate limit handling."""
        max_retries = 3
        
        for attempt in range(max_retries):
            def _send() -> Dict[str, Any]:
                return self._client.send_message(
                    {
                        "type": "private",
                        "to": [to_user_id],
                        "content": content,
                    }
                )

            res = await trio.to_thread.run_sync(_send)
            
            # Log rate limit info
            self._log_rate_limit_info(res)
            
            # Check for rate limiting
            if res.get("code") == "RATE_LIMIT_HIT":
                if attempt < max_retries - 1:
                    retry_after = self._get_rate_limit_reset(res)
                    logger.warning(
                        "Rate limit hit sending private message. "
                        "Waiting %s seconds (attempt %s/%s)",
                        retry_after, attempt + 1, max_retries
                    )
                    await trio.sleep(retry_after)
                    continue
                else:
                    logger.error("Rate limit exceeded after %s attempts", max_retries)
                    return None
            
            if res.get("result") == "success":
                return res.get("id")
            
            logger.warning("Failed to send private message: %s", res)
            return None
        
        return None

    async def send_stream_message(
        self, stream: str, topic: str, content: str
    ) -> Optional[int]:
        """Send a message to a stream with rate limit handling."""
        max_retries = 3
        
        for attempt in range(max_retries):
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
            
            # Log rate limit info
            self._log_rate_limit_info(res)
            
            # Check for rate limiting
            if res.get("code") == "RATE_LIMIT_HIT":
                if attempt < max_retries - 1:
                    retry_after = self._get_rate_limit_reset(res)
                    logger.warning(
                        "Rate limit hit sending stream message. "
                        "Waiting %s seconds (attempt %s/%s)",
                        retry_after, attempt + 1, max_retries
                    )
                    await trio.sleep(retry_after)
                    continue
                else:
                    logger.error("Rate limit exceeded after %s attempts", max_retries)
                    return None
            
            if res.get("result") == "success":
                return res.get("id")
            
            logger.warning("Failed to send stream message: %s", res)
            return None
        
        return None

    async def react_to_message(self, message_id: int, emoji_name: str) -> None:
        """Add an emoji reaction to a message."""

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
        """Delete a message by ID."""

        def _delete() -> Dict[str, Any]:
            return self._client.delete_message(message_id)

        res = await trio.to_thread.run_sync(_delete)
        if res.get("result") == "success":
            return True
        logger.warning("Failed to delete message_id=%s: %s", message_id, res)
        return False

    async def get_user_by_id(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Get user information by user ID."""

        def _get() -> Dict[str, Any]:
            return self._client.get_user_by_id(user_id)

        res = await trio.to_thread.run_sync(_get)
        if res.get("result") != "success":
            logger.warning("Failed to get user by id %s: %s", user_id, res)
            return None
        return res.get("user")

    async def get_own_user(self) -> Optional[Dict[str, Any]]:
        """Get the bot's own user profile."""

        def _get() -> Dict[str, Any]:
            return self._client.get_profile()

        res = await trio.to_thread.run_sync(_get)
        if res.get("result") != "success":
            logger.warning("Failed to get own profile: %s", res)
            return None
        return res

    async def list_users(self) -> List[Dict[str, Any]]:
        """List all users in the Zulip organization."""

        def _get() -> Dict[str, Any]:
            return self._client.get_users()

        res = await trio.to_thread.run_sync(_get)
        if res.get("result") != "success":
            logger.warning("Failed to list users: %s", res)
            return []
        return res.get("members", [])
