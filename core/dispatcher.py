"""Event dispatching system for routing Zulip messages to feature handlers.

Provides a dispatcher pattern that routes message events to registered
feature handlers based on their handling criteria.
"""
import logging
from typing import List

from core.models import MessageEvent, parse_message_event

logger = logging.getLogger(__name__)


class FeatureHandler:
    """
    Interface for feature modules.
    """

    async def handles(self, event: MessageEvent) -> bool:  # type: ignore[override]
        """Check if this handler can process the given event.
        
        Args:
            event: The message event to check
            
        Returns:
            True if this handler should process the event
        """
        raise NotImplementedError

    async def handle(self, event: MessageEvent) -> None:  # type: ignore[override]
        """Process the given event.
        
        Args:
            event: The message event to process
        """
        raise NotImplementedError


class Dispatcher:
    """Routes Zulip message events to registered feature handlers.
    
    Maintains a list of feature handlers and dispatches events to those
    that can handle them. Errors in individual handlers are isolated.
    """
    def __init__(self) -> None:
        self._features: List[FeatureHandler] = []

    def register_feature(self, feature: FeatureHandler) -> None:
        """Register a feature handler with the dispatcher.
        
        Args:
            feature: FeatureHandler instance to register
        """
        self._features.append(feature)

    async def dispatch_event(self, event_dict: dict) -> None:
        """Dispatch an event to all registered features.
        
        Parses the event and routes it to features that can handle it.
        Errors in individual features are logged but don't stop processing.
        
        Args:
            event_dict: Raw event dictionary from Zulip
        """
        msg_event = parse_message_event(event_dict)
        if msg_event is None:
            return

        for feature in self._features:
            try:
                if await feature.handles(msg_event):
                    await feature.handle(msg_event)
            except Exception:  # pylint: disable=broad-exception-caught
                # Intentionally catch all exceptions to prevent one feature
                # from crashing the entire bot
                logger.exception("Error in feature %s", feature.__class__.__name__)
