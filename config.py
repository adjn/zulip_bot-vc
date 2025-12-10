"""Configuration management for the Zulip bot.

Provides a ConfigManager class that handles loading, updating, and persisting
bot configuration from YAML files with sensible defaults.
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Dict

from storage.file_store import YAMLFileStore

logger = logging.getLogger(__name__)


DEFAULT_CONFIG: Dict[str, Any] = {
    "anonymous_posting": {
        "enabled": True,
        "target_stream": "anonymous",
        "target_topic": "general",
        # in minutes; default 7 days
        "delete_after_minutes": 7 * 24 * 60,
    },
    "private_access": {
        "enabled": True,
        "watch_rules": [
            {
                "stream": "access-requests",
                "topic": "example-topic",
                "phrase": "Default string 1",
                "target_stream": "private-room-1",
            },
            {
                "stream": "access-requests",
                "topic": "example-topic",
                "phrase": "Default string 2",
                "target_stream": "private-room-2",
            },
        ],
    },
    "logging": {
        "level": "INFO",
        "anonymize_user_ids": True,
    },
}


@dataclass
class ConfigManager:
    """Manages bot configuration with YAML file persistence.
    
    Attributes:
        path: Path to the YAML configuration file
    """
    path: str
    _store: YAMLFileStore = field(init=False)
    _config: Dict[str, Any] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self._store = YAMLFileStore(self.path)

    def load(self) -> Dict[str, Any]:
        """Load configuration from file, creating defaults if needed."""
        if not self._store.exists():
            logger.info("Config file %s not found, creating default config", self.path)
            self._store.write(DEFAULT_CONFIG)
            self._config = DEFAULT_CONFIG.copy()
            return self._config

        data = self._store.read()
        if not isinstance(data, dict):
            logger.warning("Config file malformed, resetting to defaults")
            self._store.write(DEFAULT_CONFIG)
            self._config = DEFAULT_CONFIG.copy()
            return self._config

        # Merge defaults with existing config (shallow)
        merged = DEFAULT_CONFIG.copy()
        for k, v in data.items():
            merged[k] = v
        self._config = merged
        return self._config

    def get(self) -> Dict[str, Any]:
        """Get the current configuration dictionary."""
        return self._config

    def update(self, new_config: Dict[str, Any]) -> None:
        """Update and persist the configuration.
        
        Args:
            new_config: New configuration dictionary to save
        """
        # Replace config entirely with validated dict from caller
        self._config = new_config
        self._store.write(self._config)
        logger.info("Config updated and saved to %s", self.path)
