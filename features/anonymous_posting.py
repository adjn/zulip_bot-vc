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
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from config import ConfigManager
from core.client import ClientProtocol
from core.dispatcher import FeatureHandler
from core.models import MessageEvent
from utils.scheduling import DeletionScheduler

logger = logging.getLogger(__name__)


# Conservative wildcard-mention scrub. Zulip supports several mention
# syntaxes that ping every subscriber of a stream/topic — we don't want
# anonymous posts to be able to mass-notify, so we defang them. The three
# patterns below cover:
#   1. `@**all**` / `@**everyone**` / `@**stream**` / `@**topic**` /
#      `@**channel**` — the common wildcard mentions.
#   2. `@_**all**_` (etc.) — the silent "user-mention" form Zulip renders
#      without notification visually but which still resolves server-side.
#   3. `@*role*` — role mentions (e.g. `@*moderators*`); we don't try to
#      enumerate role names, we just neutralise the syntax.
# For each match we replace the leading ASCII '@' with the fullwidth '＠'
# (U+FF20), which looks identical to a human reader but isn't recognised
# by Zulip's mention parser.
_WILDCARD_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"@\*\*(all|everyone|stream|topic|channel)\*\*", re.IGNORECASE),
    re.compile(r"@_\*\*(all|everyone|stream|topic|channel)\*\*_", re.IGNORECASE),
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
class PendingAnon:
    """A pending anonymous post awaiting user confirmation."""

    original_content: str
    confirmation_message_id: int | None
    expires_at: datetime


@dataclass
class AnonymousPostingFeature(FeatureHandler):
    """Handles anonymous posting via DM."""

    client: ClientProtocol
    config_mgr: ConfigManager
    scheduler: DeletionScheduler
    _pending: dict[int, PendingAnon] = field(default_factory=dict, repr=False)
    _last_post_at: dict[int, datetime] = field(default_factory=dict, repr=False)

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
        # The flow has two phases driven by `_pending` membership:
        #   1. New flow: this user has no pending submission. We treat the
        #      DM as their proposed anonymous content, send a confirmation
        #      prompt back, and record it in `_pending`.
        #   2. Confirmation: the user already has a pending submission.
        #      Their next DM must be SEND (relay it), CANCEL (drop it), or
        #      anything else (clear pending and ask them to restart).
        # The TTL eviction below means an abandoned prompt won't trap the
        # user's *next* unrelated DM as accidental SEND/CANCEL input.
        cfg = self._cfg()
        target_stream: str = cfg.get("target_stream", "anonymous")
        target_topic: str = cfg.get("target_topic", "general")
        delete_after_minutes: int = cfg.get("delete_after_minutes", 7 * 24 * 60)
        max_len: int = int(cfg.get("max_content_length", 4000))
        cooldown: int = int(cfg.get("min_seconds_between_posts", 30))
        scrub: bool = bool(cfg.get("scrub_wildcard_mentions", True))
        pending_ttl: int = int(cfg.get("pending_ttl_minutes", 10))

        normalized = event.content.strip().lower()
        self._evict_expired_pending()

        # --- confirmation step ---------------------------------------
        if event.sender_id in self._pending:
            pending = self._pending.pop(event.sender_id)

            if normalized == "send":
                await self._post_anonymously(
                    pending=pending,
                    sender_id=event.sender_id,
                    target_stream=target_stream,
                    target_topic=target_topic,
                    delete_after_minutes=delete_after_minutes,
                    max_len=max_len,
                    scrub=scrub,
                )
                return

            if normalized == "cancel":
                if pending.confirmation_message_id is not None:
                    await self.scheduler.schedule_deletion(
                        message_id=pending.confirmation_message_id,
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
        # Per-sender cooldown
        last = self._last_post_at.get(event.sender_id)
        if last is not None:
            elapsed = (_now_utc() - last).total_seconds()
            if elapsed < cooldown:
                wait = int(cooldown - elapsed) + 1
                await self.client.send_private_message(
                    event.sender_id,
                    f"Please wait {wait}s before posting again.",
                )
                return

        original = event.content
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

        self._pending[event.sender_id] = PendingAnon(
            original_content=original,
            confirmation_message_id=confirmation_msg_id,
            expires_at=_now_utc() + timedelta(minutes=pending_ttl),
        )

    # ---------------------------------------------------------------- helpers

    async def _post_anonymously(
        self,
        *,
        pending: PendingAnon,
        sender_id: int,
        target_stream: str,
        target_topic: str,
        delete_after_minutes: int,
        max_len: int,
        scrub: bool,
    ) -> None:
        body = pending.original_content
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
            self._last_post_at[sender_id] = _now_utc()

        if pending.confirmation_message_id is not None:
            await self.scheduler.schedule_deletion(
                message_id=pending.confirmation_message_id,
                delete_after_minutes=1,
            )

    def _evict_expired_pending(self) -> None:
        now = _now_utc()
        stale = [uid for uid, p in self._pending.items() if p.expires_at <= now]
        for uid in stale:
            self._pending.pop(uid, None)
