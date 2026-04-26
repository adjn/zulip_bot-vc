"""Private access control feature for the Zulip bot.

Watches `(stream, topic)` for an exact (case/whitespace-insensitive)
trigger phrase; on match, subscribes the sender to a target stream and
reacts with :saluting_face:.

This is intentionally a low-friction self-subscription mechanism. It is
NOT access control in any meaningful sense — anyone who learns the phrase
can self-subscribe. For a stronger model, gate the subscription on
admin approval (tracked as a long-term recommendation).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from config import ConfigManager
from core.client import ClientProtocol
from core.dispatcher import FeatureHandler
from core.models import MessageEvent
from utils.matching import normalize_phrase

logger = logging.getLogger(__name__)


@dataclass
class WatchRule:
    stream: str
    topic: str
    phrase: str
    target_stream: str


@dataclass
class PrivateAccessFeature(FeatureHandler):
    client: ClientProtocol
    config_mgr: ConfigManager
    # Cache of parsed rules, keyed by ConfigManager.version. We rebuild only
    # when the config has actually changed; on a busy stream this turns the
    # per-message rule walk into a dict lookup. `None` means "never primed".
    _cached_version: int | None = field(default=None, init=False, repr=False)
    _cached_rules: list[WatchRule] = field(default_factory=list, init=False, repr=False)

    def _load_rules(self) -> list[WatchRule]:
        # Fast path: config hasn't changed since the last parse.
        version = self.config_mgr.version
        if self._cached_version == version:
            return self._cached_rules

        cfg = self.config_mgr.get().get("private_access", {})
        if not cfg.get("enabled", False):
            self._cached_version = version
            self._cached_rules = []
            return self._cached_rules

        rules_conf: list[dict[str, Any]] = cfg.get("watch_rules", []) or []
        rules: list[WatchRule] = []
        for r in rules_conf:
            try:
                rules.append(
                    WatchRule(
                        stream=r["stream"],
                        topic=r["topic"],
                        phrase=r["phrase"],
                        target_stream=r["target_stream"],
                    )
                )
            except KeyError:
                # Logged once per config version (not once per message)
                # because we're inside the cache-miss branch.
                logger.warning("Invalid watch rule in config: %s", r)

        self._cached_version = version
        self._cached_rules = rules
        return self._cached_rules

    def _anonymize_logging(self) -> bool:
        return bool(self.config_mgr.get().get("logging", {}).get("anonymize_user_ids", False))

    async def handles(self, event: MessageEvent) -> bool:
        if event.message_type != "stream":
            return False
        rules = self._load_rules()
        if not rules:
            return False
        return any(r.stream == event.stream and r.topic == event.topic for r in rules)

    async def handle(self, event: MessageEvent) -> None:
        rules = self._load_rules()
        if not rules:
            return

        msg_norm = normalize_phrase(event.content)
        anonymize = self._anonymize_logging()
        sender_repr: object = "<redacted>" if anonymize else event.sender_id

        for r in rules:
            if r.stream != event.stream or r.topic != event.topic:
                continue
            if normalize_phrase(r.phrase) != msg_norm:
                continue

            logger.info(
                "PrivateAccess: subscribing sender_id=%s to target_stream=%s",
                sender_repr,
                r.target_stream,
            )
            await self.client.add_user_subscriptions(
                user_id=event.sender_id, streams=[r.target_stream]
            )
            await self.client.react_to_message(event.id, "saluting_face")
