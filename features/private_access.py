import logging
from dataclasses import dataclass
from typing import Any, Dict, List

from config import ConfigManager
from core.client import ZulipTrioClient
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


class PrivateAccessFeature(FeatureHandler):
    """
    Watches specific (stream, topic) threads for trigger phrases and
    subscribes users to target streams, reacting with :saluting_face:.
    """

    def __init__(
        self,
        client: ZulipTrioClient,
        config_mgr: ConfigManager,
    ) -> None:
        self.client = client
        self.config_mgr = config_mgr

    def _load_rules(self) -> List[WatchRule]:
        cfg = self.config_mgr.get().get("private_access", {})
        if not cfg.get("enabled", False):
            return []
        rules_conf: List[Dict[str, Any]] = cfg.get("watch_rules", [])
        rules: List[WatchRule] = []
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
                logger.warning("Invalid watch rule in config: %s", r)
        return rules

    async def handles(self, event: MessageEvent) -> bool:
        if event.message_type != "stream":
            return False
        rules = self._load_rules()
        if not rules:
            return False
        # quick check if this (stream, topic) is relevant at all
        for r in rules:
            if (r.stream == event.stream) and (r.topic == event.topic):
                return True
        return False

    async def handle(self, event: MessageEvent) -> None:
        rules = self._load_rules()
        if not rules:
            return

        msg_norm = normalize_phrase(event.content)

        for r in rules:
            if r.stream != event.stream or r.topic != event.topic:
                continue
            if normalize_phrase(r.phrase) == msg_norm:
                logger.info(
                    "PrivateAccess: subscribing sender_id=%s to target_stream=%s due to phrase match",
                    event.sender_id,
                    r.target_stream,
                )
                # Subscribe this user to the target stream
                await self.client.add_user_subscriptions(
                    user_id=event.sender_id,
                    streams=[r.target_stream],
                )
                # React with saluting_face
                await self.client.react_to_message(event.id, "saluting_face")