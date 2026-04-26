"""Welcome DM feature.

When a new user joins the realm, schedule a welcome DM after a
configurable delay. The delay exists so the welcome lands *after*
whatever join-flow the realm itself runs (admin DMs, "intro" stream
posts, etc.) rather than colliding with them.

Durability + safety properties:

* The pending welcome lives in SQLite (``pending_welcomes`` table), so a
  bot restart between "user joined" and "delay elapsed" still delivers.
* Storage insert uses ``ON CONFLICT DO NOTHING``, so a duplicate
  ``realm_user.add`` event (e.g. after a queue reconnect) doesn't double
  the welcome — the first scheduled time wins.
* The polling loop is owned by :class:`core.scheduler.Scheduler`; this
  module only contributes a ``tick()``.
* Bot users are filtered upstream (in :class:`core.dispatcher.Dispatcher`),
  so we don't try to welcome ourselves on first-event-after-restart.

Config (``welcome`` section):

* ``enabled`` (bool, default False) — master switch.
* ``delay_minutes`` (int, default 5) — wait this long after join before
  DMing.
* ``message`` (str) — the welcome body. ``{user_id}`` and ``{mention}``
  placeholders are substituted; everything else is sent verbatim.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from config import ConfigManager
from core.client import ClientProtocol
from core.context import FeatureContext
from storage.db import Storage

logger = logging.getLogger(__name__)


# Per-typo-defense bounds — not policy. Keeps a fat-fingered config
# value (negative, decade-long delay) from quietly breaking the feature.
_DELAY_MINUTES_MIN = 0
_DELAY_MINUTES_MAX = 7 * 24 * 60  # one week


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class WelcomeFeature:
    """Schedules and delivers welcome DMs to newly-joined users."""

    ctx: FeatureContext

    @property
    def client(self) -> ClientProtocol:
        return self.ctx.client

    @property
    def config_mgr(self) -> ConfigManager:
        return self.ctx.config_mgr

    @property
    def storage(self) -> Storage:
        # Welcome is durable; storage is required, not optional.
        assert self.ctx.storage is not None, "WelcomeFeature requires storage"
        return self.ctx.storage

    def _settings(self) -> dict[str, Any]:
        return self.config_mgr.get().get("welcome", {}) or {}

    def _delay_minutes(self) -> int:
        raw = self._settings().get("delay_minutes", 5)
        try:
            n = int(raw)
        except (TypeError, ValueError):
            logger.warning("welcome.delay_minutes not an int (%r); using default 5", raw)
            return 5
        # Clamp rather than reject. The feature is opt-in via `enabled`;
        # if someone has weird values, we'd rather still send a sensible
        # welcome than silently no-op.
        if n < _DELAY_MINUTES_MIN:
            return _DELAY_MINUTES_MIN
        if n > _DELAY_MINUTES_MAX:
            return _DELAY_MINUTES_MAX
        return n

    def _enabled(self) -> bool:
        return bool(self._settings().get("enabled", False))

    # --- realm_user.add hook -------------------------------------------

    async def on_user_added(self, user_id: int) -> None:
        """Schedule a welcome DM for *user_id*.

        Wired up in ``bot_main.py`` via
        ``dispatcher.register_realm_user_add_handler``.
        """
        if not self._enabled():
            return
        deliver_at = _utcnow() + timedelta(minutes=self._delay_minutes())
        await self.storage.schedule_welcome(user_id, deliver_at)
        logger.info("Welcome scheduled user_id=%s at %s", user_id, deliver_at.isoformat())

    # --- scheduler tick ------------------------------------------------

    async def tick(self) -> None:
        """Deliver all welcomes whose deadline has passed."""
        if not self._enabled():
            # When disabled at runtime we still drain pending rows: the
            # admin's intent is "stop welcoming people", and leaving a
            # pile of stale rows that fire if the flag flips back on
            # would surprise everyone. Cheap operation; runs once per
            # poll interval.
            now = _utcnow()
            dropped = await self.storage.claim_due_welcomes(now)
            if dropped:
                logger.info("Welcome disabled; dropped %d due rows", len(dropped))
            return

        now = _utcnow()
        due = await self.storage.claim_due_welcomes(now)
        if not due:
            return
        body = self._settings().get(
            "message",
            "Hi! Welcome to the realm. :wave:",
        )
        for user_id in due:
            content = self._render(body, user_id)
            try:
                msg_id = await self.client.send_private_message(user_id, content)
            except Exception:
                logger.exception("Welcome send raised user_id=%s", user_id)
                continue
            if msg_id is None:
                logger.warning("Welcome send failed user_id=%s; not retrying", user_id)
            else:
                logger.info("Welcome delivered user_id=%s msg_id=%s", user_id, msg_id)

    @staticmethod
    def _render(template: str, user_id: int) -> str:
        """Substitute the small set of allowed placeholders.

        Only ``{user_id}`` and ``{mention}`` are substituted. Anything
        else (curly braces in the template, unknown keys) is left alone
        so a stray ``{`` in the welcome message can't crash delivery.
        """
        try:
            return template.format(user_id=user_id, mention=f"@_**|{user_id}**")
        except (KeyError, IndexError, ValueError):
            logger.warning("welcome.message has bad placeholders; sending raw")
            return template

    # --- inspection ----------------------------------------------------

    async def pending_count(self) -> int:
        return await self.storage.pending_welcome_count()
