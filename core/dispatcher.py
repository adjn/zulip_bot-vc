import logging
from typing import List

from core.models import MessageEvent, parse_message_event

logger = logging.getLogger(__name__)


class FeatureHandler:
    """
    Interface for feature modules.
    """

    async def handles(self, event: MessageEvent) -> bool:  # type: ignore[override]
        raise NotImplementedError

    async def handle(self, event: MessageEvent) -> None:  # type: ignore[override]
        raise NotImplementedError


class Dispatcher:
    def __init__(self) -> None:
        self._features: List[FeatureHandler] = []

    def register_feature(self, feature: FeatureHandler) -> None:
        self._features.append(feature)

    async def dispatch_event(self, event_dict: dict) -> None:
        msg_event = parse_message_event(event_dict)
        if msg_event is None:
            return

        for feature in self._features:
            try:
                if await feature.handles(msg_event):
                    await feature.handle(msg_event)
            except Exception:  # pragma: no cover - protective
                logger.exception("Error in feature %s", feature.__class__.__name__)