"""Anonymous posting feature for the Zulip bot.

A user DMs the bot with content; the bot replies with a confirmation that
includes a length-limited preview, escapes backticks so the preview cannot
break out of its code fence, and waits for ``SEND`` or ``CANCEL``. On
``SEND`` the bot relays the (sanitized) content to a configured
stream/topic and schedules the relayed message for deletion.

Abuse mitigation:

* Per-sender cooldown (``min_seconds_between_posts``).
* Hard cap on outgoing content length (``max_content_length``).
* Wildcard-mention scrub (``@all`` / ``@everyone`` / ``@stream`` /
  ``@topic`` / wildcard role mentions are neutralised so anonymous posts
  cannot mass-notify).
* TTL on pending confirmations (``pending_ttl_minutes``) so an abandoned
  flow doesn't trap the user's next DM as ``SEND`` / ``CANCEL``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from config import ConfigManager
from core.client import ClientProtocol
from core.context import FeatureContext
from core.dispatcher import FeatureHandler
from core.models import MessageEvent
from storage.db import Storage
from utils.scheduling import DeletionScheduler

logger = logging.getLogger(__name__)


# Conservative wildcard-mention scrub. Zulip supports several mention
# syntaxes that ping every subscriber of a stream/topic — we don't want
# anonymous posts to be able to mass-notify, so we defang them. The
# patterns below cover:
#   1. `@**all**` / `@**everyone**` / `@**stream**` / `@**topic**` /
#      `@**channel**` — the common wildcard mentions.
#   2. `@_**all**_` (etc.) — the silent "user-mention" form Zulip renders
#      without notification visually but which still resolves server-side.
#   3. `@*role*` — role mentions (e.g. `@*moderators*`); we don't try to
#      enumerate role names, we just neutralise the syntax.
#   4. `@_*role*_` — the silent variant of role mentions; same defense.
# For each match we replace the leading ASCII '@' with the fullwidth '＠'
# (U+FF20), which looks identical to a human reader but isn't recognised
# by Zulip's mention parser.
_WILDCARD_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"@\*\*(all|everyone|stream|topic|channel)\*\*", re.IGNORECASE),
    re.compile(r"@_\*\*(all|everyone|stream|topic|channel)\*\*_", re.IGNORECASE),
    re.compile(r"@_\*[^*]+\*_"),
    re.compile(r"@\*[^*]+\*"),
)


def _scrub_wildcards(text: str) -> str:
    """Defang wildcard mentions so anonymous posts can't mass-notify."""

    def _replace(match: re.Match[str]) -> str:
        # Replace the literal '@' with a U+FF20 fullwidth '＠' so the text
        # remains readable, but Zulip's mention parser won't fire.
        return "＠" + match.group(0)[1:]

    out = text
    for pat in _WILDCARD_PATTERNS:
        out = pat.sub(_replace, out)
    return out


def _escape_for_code_fence(text: str) -> str:
    """Make ``text`` safe to embed inside a triple-backtick code fence.

    Replaces backtick runs of length >= 3 with U+02CB grave-accent runs of
    the same length, preserving the visual approximation while preventing
    the fence from being broken.
    """
    return re.sub(r"`{3,}", lambda m: "\u02cb" * len(m.group(0)), text)


def _now_utc() -> datetime:
    return datetime.now(UTC)


@dataclass
class AnonymousPostingFeature(FeatureHandler):
    """Handles anonymous posting via DM.

    Pending confirmations and per-sender cooldowns live in
    :class:`Storage` so they survive a bot restart -- the auto-delete
    privacy contract depends on it.
    """

    ctx: FeatureContext

    # Read-only views over `ctx`. Method bodies use `self.client` etc. just
    # as before; the property layer keeps the diff small and lets mypy see
    # narrowed (non-Optional) types for the deps we always require.
    @property
    def client(self) -> ClientProtocol:
        return self.ctx.client

    @property
    def config_mgr(self) -> ConfigManager:
        return self.ctx.config_mgr

    @property
    def scheduler(self) -> DeletionScheduler:
        scheduler = self.ctx.scheduler
        assert scheduler is not None, "AnonymousPostingFeature requires ctx.scheduler"
        return scheduler

    @property
    def storage(self) -> Storage:
        storage = self.ctx.storage
        assert storage is not None, "AnonymousPostingFeature requires ctx.storage"
        return storage

    # ---------------------------------------------------------------- guards

    def _cfg(self) -> dict[str, Any]:
        return self.config_mgr.get().get("anonymous_posting", {})

    async def handles(self, event: MessageEvent) -> bool:
        cfg = self._cfg()
        if not cfg.get("enabled", False):
            return False
        if event.message_type != "private":
            return False
        # Admin commands take precedence and are routed elsewhere.
        return not event.content.strip().startswith("!")

    # ---------------------------------------------------------------- handler

    async def handle(self, event: MessageEvent) -> None:
        # The flow has two phases driven by whether this sender has a
        # pending row in the DB:
        #   1. New flow: no pending row. We treat the DM as their
        #      proposed anonymous content, send a confirmation prompt,
        #      and upsert a pending row.
        #   2. Confirmation: a non-expired pending row exists. The next
        #      DM must be SEND (relay it), CANCEL (drop it), or anything
        #      else (drop the pending row and ask them to restart).
        # `fetch_pending` evicts expired rows itself, so an abandoned
        # prompt can't trap the user's *next* unrelated DM.
        cfg = self._cfg()
        target_stream: str = cfg.get("target_stream", "anonymous")
        target_topic: str = cfg.get("target_topic", "general")
        delete_after_minutes: int = cfg.get("delete_after_minutes", 7 * 24 * 60)
        max_len: int = int(cfg.get("max_content_length", 4000))
        cooldown: int = int(cfg.get("min_seconds_between_posts", 30))
        scrub: bool = bool(cfg.get("scrub_wildcard_mentions", True))
        pending_ttl: int = int(cfg.get("pending_ttl_minutes", 10))

        normalized = event.content.strip().lower()
        now = _now_utc()
        pending = await self.storage.fetch_pending(event.sender_id, now=now)

        # --- confirmation step ---------------------------------------
        if pending is not None:
            popped = await self.storage.pop_pending(event.sender_id)
            # popped is logically the same row we just fetched; we use
            # pop_pending to delete it atomically rather than racing.
            original_content, confirmation_message_id = popped or pending

            if normalized == "send":
                await self._post_anonymously(
                    original_content=original_content,
                    confirmation_message_id=confirmation_message_id,
                    sender_id=event.sender_id,
                    target_stream=target_stream,
                    target_topic=target_topic,
                    delete_after_minutes=delete_after_minutes,
                    max_len=max_len,
                    scrub=scrub,
                )
                return

            if normalized == "cancel":
                if confirmation_message_id is not None:
                    await self.scheduler.schedule_deletion(
                        message_id=confirmation_message_id,
                        delete_after_minutes=1,
                    )
                await self.client.send_private_message(event.sender_id, "Cancelled.")
                return

            await self.client.send_private_message(
                event.sender_id,
                "Unknown input. Please start over by sending your message again.",
            )
            return

        # --- new flow -------------------------------------------------
        # Reject empty / whitespace-only submissions before doing
        # anything else: neither the cooldown clock nor the pending row
        # should advance for a "message" with no content.
        original = event.content
        if not original.strip():
            await self.client.send_private_message(
                event.sender_id,
                "Your message is empty. Type some content and try again.",
            )
            return

        # Per-sender cooldown
        last = await self.storage.fetch_cooldown(event.sender_id)
        if last is not None:
            elapsed = (now - last).total_seconds()
            if elapsed < cooldown:
                wait = int(cooldown - elapsed) + 1
                await self.client.send_private_message(
                    event.sender_id,
                    f"Please wait {wait}s before posting again.",
                )
                # Note: we deliberately do NOT audit-log throttle hits —
                # an audit row keyed by sender_id is the same privacy
                # leak as auditing successful submissions, which we
                # explicitly avoid (see core/audit.py docstring).
                logger.info("anon throttled (sender_id redacted)")
                return

        if len(original) > max_len:
            await self.client.send_private_message(
                event.sender_id,
                f"Message is {len(original)} characters; max is {max_len}. "
                "Please shorten and resend.",
            )
            return

        preview = _escape_for_code_fence(original.strip())
        if len(preview) > 500:
            preview = preview[:500] + " ..."

        confirmation_msg_id = await self.client.send_private_message(
            event.sender_id,
            (
                "You wrote:\n\n"
                f"```text\n{preview}\n```\n\n"
                "Reply with `SEND` to post anonymously, or `CANCEL` to discard."
            ),
        )

        await self.storage.upsert_pending(
            sender_id=event.sender_id,
            original_content=original,
            confirmation_message_id=confirmation_msg_id,
            expires_at=now + timedelta(minutes=pending_ttl),
        )

    # ---------------------------------------------------------------- helpers

    async def _post_anonymously(
        self,
        *,
        original_content: str,
        confirmation_message_id: int | None,
        sender_id: int,
        target_stream: str,
        target_topic: str,
        delete_after_minutes: int,
        max_len: int,
        scrub: bool,
    ) -> None:
        body = original_content
        if len(body) > max_len:
            body = body[:max_len]
        if scrub:
            body = _scrub_wildcards(body)
        content = f"Anonymous message:\n\n{body}"

        anon_msg_id = await self.client.send_stream_message(target_stream, target_topic, content)
        if anon_msg_id is not None:
            await self.scheduler.schedule_deletion(
                message_id=anon_msg_id,
                delete_after_minutes=delete_after_minutes,
            )
            await self.storage.upsert_cooldown(sender_id, _now_utc())

        if confirmation_message_id is not None:
            await self.scheduler.schedule_deletion(
                message_id=confirmation_message_id,
                delete_after_minutes=1,
            )
