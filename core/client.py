"""Trio-friendly wrapper for the Zulip API client.

Design notes
------------

* All blocking SDK calls go through ``trio.to_thread.run_sync`` with
  ``abandon_on_cancel=True`` so nursery teardown / Ctrl-C is prompt.
* The Zulip Python SDK returns the parsed JSON body only, so HTTP
  ``X-RateLimit-*`` headers are *not* surfaced. We honour the JSON
  ``code == "RATE_LIMIT_HIT"`` branch (and the body's ``retry-after``
  field when present) and rely on the body for backoff. Any "header-aware"
  rate-limit telemetry must be added at the transport layer (subclass
  ``zulip.Client`` and capture ``response.headers``).
* The event loop signals queue invalidation via :class:`QueueInvalidated`;
  the caller is expected to re-register and resume with a fresh queue.
"""

from __future__ import annotations

import logging
import os
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import Any, Protocol

import trio
import zulip

logger = logging.getLogger(__name__)


# Maximum number of retries on transient rate-limit/network errors for
# user-facing API calls. The long-poll loop has its own retry policy.
_MAX_RETRIES = 3


class QueueInvalidated(Exception):
    """Raised by :meth:`ZulipTrioClient.events` when the event queue must be
    re-registered (Zulip code ``BAD_EVENT_QUEUE_ID``)."""


class ClientProtocol(Protocol):
    """The subset of :class:`ZulipTrioClient` that features depend on.

    Tests use a fake matching this shape; production code uses the real
    client. Keep this surface minimal.
    """

    async def send_private_message(self, to_user_id: int, content: str) -> int | None: ...
    async def send_stream_message(self, stream: str, topic: str, content: str) -> int | None: ...
    async def react_to_message(self, message_id: int, emoji_name: str) -> None: ...
    async def add_user_subscriptions(self, user_id: int, streams: Iterable[str]) -> None: ...
    async def subscribe_bot_to_streams(self, streams: Iterable[str]) -> dict[str, Any]: ...
    async def delete_message(self, message_id: int) -> bool: ...
    async def get_user_by_id(self, user_id: int) -> dict[str, Any] | None: ...


class ZulipTrioClient:
    """Trio-friendly wrapper around :class:`zulip.Client`."""

    def __init__(self, client: zulip.Client) -> None:
        self._client = client

    # --- construction --------------------------------------------------

    @classmethod
    def from_env_or_rc(cls) -> ZulipTrioClient:
        """Build a client from ``ZULIP_CONFIG_FILE`` or the default ``~/.zuliprc``."""
        config_file = os.environ.get("ZULIP_CONFIG_FILE")
        client = zulip.Client(config_file=config_file) if config_file else zulip.Client()
        return cls(client)

    # --- internal helpers ---------------------------------------------

    @staticmethod
    async def _to_thread(fn: Callable[[], Any]) -> Any:
        """Run a blocking call in a thread, with prompt cancellation.

        ``abandon_on_cancel=True`` lets trio give up on the thread if the
        nursery is cancelled (e.g. Ctrl-C) instead of waiting for the
        90-second long-poll timeout to expire. The thread keeps running in
        the background and its result is discarded — fine for idempotent
        reads; we never use this for non-idempotent writes that can't be
        safely abandoned.
        """
        return await trio.to_thread.run_sync(fn, abandon_on_cancel=True)

    async def _call_with_retries(
        self,
        op_name: str,
        fn: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        """Invoke ``fn`` (a blocking SDK call) honouring ``RATE_LIMIT_HIT``.

        Retries up to :data:`_MAX_RETRIES` times on rate-limit responses,
        sleeping for the body's ``retry-after`` (if present) plus jitter.
        Returns the final response dict (caller inspects ``result``).
        """
        for attempt in range(_MAX_RETRIES):
            res = await self._to_thread(fn)
            if res.get("code") != "RATE_LIMIT_HIT":
                return res

            wait = self._retry_after_seconds(res)
            if attempt < _MAX_RETRIES - 1:
                logger.warning(
                    "%s rate-limited; sleeping %.1fs (attempt %d/%d): %s",
                    op_name,
                    wait,
                    attempt + 1,
                    _MAX_RETRIES,
                    res.get("msg", ""),
                )
                await trio.sleep(wait)
                continue
            logger.error(
                "%s rate-limited after %d attempts: %s",
                op_name,
                _MAX_RETRIES,
                res.get("msg", ""),
            )
            return res

        # Unreachable: the loop above either returns inside the loop body
        # or returns on the final attempt. mypy can't prove that, so we
        # need this final return to satisfy the type checker.
        return {"result": "error", "msg": "unreachable"}

    @staticmethod
    def _retry_after_seconds(res: dict[str, Any]) -> float:
        """Read ``retry-after`` from a 429-shaped JSON body, with a default."""
        raw = res.get("retry-after")
        try:
            wait = float(raw) if raw is not None else 60.0
        except (TypeError, ValueError):
            wait = 60.0
        # Add a small jitter to avoid thundering-herd retries.
        return max(1.0, wait + random.uniform(0, 1.0))

    @staticmethod
    def _result_ok(res: dict[str, Any]) -> bool:
        return res.get("result") == "success"

    # --- event queue ---------------------------------------------------

    async def register(self, **kwargs: Any) -> dict[str, Any]:
        """Register an event queue. Raises ``RuntimeError`` if the API errors."""
        res = await self._to_thread(lambda: self._client.register(**kwargs))
        if not self._result_ok(res):
            raise RuntimeError(f"Failed to register event queue: {res.get('msg', res)}")
        return res

    async def events(self, queue: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """Yield events from a long-poll loop on a registered queue.

        Raises :class:`QueueInvalidated` when the server reports
        ``BAD_EVENT_QUEUE_ID``. The caller should call :meth:`register`
        again and re-enter ``events()``.
        """
        queue_id = queue["queue_id"]
        last_event_id = queue["last_event_id"]

        while True:
            try:
                # Capture the current `last_event_id` value into a default
                # argument of the inner function. This forces an
                # iteration-time bind rather than the usual Python "look it
                # up in the enclosing scope when called" semantics — which
                # would race with the `last_event_id = ...` update at the
                # bottom of this loop. Looks redundant; isn't.
                current_last: int = last_event_id

                def _get_events(lid: int = current_last) -> dict[str, Any]:
                    return self._client.get_events(
                        queue_id=queue_id,
                        last_event_id=lid,
                        dont_block=False,
                        timeout=90,
                    )

                res = await self._to_thread(_get_events)
            except (OSError, ConnectionError, TimeoutError) as e:
                logger.warning("Network error in event loop: %s", e)
                await trio.sleep(5)
                continue

            code = res.get("code")
            if code == "BAD_EVENT_QUEUE_ID":
                logger.info("Event queue invalidated; re-registering")
                raise QueueInvalidated()
            if code == "RATE_LIMIT_HIT":
                wait = self._retry_after_seconds(res)
                logger.warning("Rate-limit hit on get_events; sleeping %.1fs", wait)
                await trio.sleep(wait)
                continue

            if not self._result_ok(res):
                logger.warning(
                    "get_events error code=%s msg=%s",
                    code,
                    res.get("msg"),
                )
                await trio.sleep(5)
                continue

            for event in res.get("events", []):
                event_id = event.get("id")
                if isinstance(event_id, int):
                    last_event_id = max(last_event_id, event_id)
                yield event

    # --- send / react / subscribe / delete -----------------------------

    async def send_private_message(self, to_user_id: int, content: str) -> int | None:
        res = await self._call_with_retries(
            "send_private_message",
            lambda: self._client.send_message(
                {"type": "private", "to": [to_user_id], "content": content}
            ),
        )
        if self._result_ok(res):
            msg_id = res.get("id")
            return msg_id if isinstance(msg_id, int) else None
        logger.warning("send_private_message failed: %s", res.get("msg"))
        return None

    async def send_stream_message(self, stream: str, topic: str, content: str) -> int | None:
        res = await self._call_with_retries(
            "send_stream_message",
            lambda: self._client.send_message(
                {
                    "type": "stream",
                    "to": stream,
                    "topic": topic,
                    "content": content,
                }
            ),
        )
        if self._result_ok(res):
            msg_id = res.get("id")
            return msg_id if isinstance(msg_id, int) else None
        logger.warning("send_stream_message failed: %s", res.get("msg"))
        return None

    async def react_to_message(self, message_id: int, emoji_name: str) -> None:
        res = await self._call_with_retries(
            "react_to_message",
            lambda: self._client.add_reaction({"message_id": message_id, "emoji_name": emoji_name}),
        )
        if not self._result_ok(res):
            logger.warning("react_to_message failed: %s", res.get("msg"))

    async def add_user_subscriptions(self, user_id: int, streams: Iterable[str]) -> None:
        names = list(streams)
        if not names:
            return
        res = await self._call_with_retries(
            "add_user_subscriptions",
            lambda: self._client.add_subscriptions(
                streams=[{"name": s} for s in names],
                principals=[user_id],
            ),
        )
        if not self._result_ok(res):
            logger.warning(
                "add_user_subscriptions(user=%s) failed: %s",
                user_id,
                res.get("msg"),
            )

    async def subscribe_bot_to_streams(self, streams: Iterable[str]) -> dict[str, Any]:
        names = list(streams)
        if not names:
            return {"result": "error", "msg": "No streams provided"}
        return await self._call_with_retries(
            "subscribe_bot_to_streams",
            lambda: self._client.add_subscriptions(streams=[{"name": s} for s in names]),
        )

    async def delete_message(self, message_id: int) -> bool:
        res = await self._call_with_retries(
            "delete_message",
            lambda: self._client.delete_message(message_id),
        )
        if self._result_ok(res):
            return True
        # Permission errors on user-owned DMs are routine; debug-log them.
        msg = (res.get("msg") or "").lower()
        if res.get("code") == "BAD_REQUEST" and "permission" in msg:
            logger.debug(
                "No permission to delete message_id=%s (expected for DMs)",
                message_id,
            )
        else:
            logger.warning(
                "delete_message(id=%s) failed: code=%s msg=%s",
                message_id,
                res.get("code"),
                res.get("msg"),
            )
        return False

    # --- profile / users -----------------------------------------------

    async def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        res = await self._call_with_retries(
            "get_user_by_id",
            lambda: self._client.get_user_by_id(user_id),
        )
        if not self._result_ok(res):
            logger.warning("get_user_by_id(%s) failed: %s", user_id, res.get("msg"))
            return None
        user = res.get("user")
        return user if isinstance(user, dict) else None

    async def get_own_user(self) -> dict[str, Any] | None:
        res = await self._call_with_retries(
            "get_own_user",
            self._client.get_profile,
        )
        if not self._result_ok(res):
            logger.warning("get_own_user failed: %s", res.get("msg"))
            return None
        return res

    async def list_users(self) -> list[dict[str, Any]]:
        res = await self._call_with_retries(
            "list_users",
            self._client.get_users,
        )
        if not self._result_ok(res):
            logger.warning("list_users failed: %s", res.get("msg"))
            return []
        members = res.get("members", [])
        return members if isinstance(members, list) else []


# Helper used by call sites that want to await an arbitrary Awaitable.
async def _await(aw: Awaitable[Any]) -> Any:  # pragma: no cover
    return await aw
