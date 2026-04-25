"""Configuration management for the Zulip bot.

Loads / persists YAML config with deep-copied defaults and a deep-merge.
All feature modules ship `enabled: False` by default — first-run is a no-op.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any

from storage.file_store import YAMLFileStore

logger = logging.getLogger(__name__)


# All features default-disabled. Operators must opt in via config.yaml or
# admin commands.
DEFAULT_CONFIG: dict[str, Any] = {
    "anonymous_posting": {
        "enabled": False,
        "target_stream": "anonymous",
        "target_topic": "general",
        "delete_after_minutes": 7 * 24 * 60,
        # Abuse mitigation
        "max_content_length": 4000,
        "min_seconds_between_posts": 30,
        "scrub_wildcard_mentions": True,
        "pending_ttl_minutes": 10,
    },
    "private_access": {
        "enabled": False,
        "watch_rules": [
            {
                "stream": "access-requests",
                "topic": "example-topic",
                "phrase": "Default string 1",
                "target_stream": "private-room-1",
            },
        ],
    },
    "admin": {
        # Optional explicit super-admin allowlist of Zulip user_ids.
        # When non-empty, super-admin commands additionally require membership.
        "super_admin_user_ids": [],
        # How long to cache an admin/owner role lookup before re-checking.
        "role_cache_ttl_seconds": 60,
    },
    "logging": {
        "level": "INFO",
        "anonymize_user_ids": False,
    },
}


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `overlay` into a deep copy of `base`.

    Lists and scalars in `overlay` replace their counterparts; nested dicts
    merge key-by-key. The original inputs are not mutated.

    Example::

        base    = {"a": {"x": 1, "y": 2}, "b": 3}
        overlay = {"a": {"y": 20}}
        result  = {"a": {"x": 1, "y": 20}, "b": 3}

    This matters because user `config.yaml` files typically only set the
    keys the operator cares about; we want unspecified nested keys (like
    `delete_after_minutes`) to keep their default values rather than
    disappear.
    """
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


@dataclass
class ConfigManager:
    """Manages bot configuration with YAML file persistence."""

    path: str
    _store: YAMLFileStore = field(init=False)
    _config: dict[str, Any] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self._store = YAMLFileStore(self.path)

    def load(self) -> dict[str, Any]:
        """Load configuration from file, creating defaults if needed."""
        if not self._store.exists():
            logger.info("Config file %s not found, creating default config", self.path)
            self._config = copy.deepcopy(DEFAULT_CONFIG)
            self._store.write(self._config)
            return self._config

        data = self._store.read()
        if not isinstance(data, dict):
            logger.warning("Config file malformed, resetting to defaults")
            self._config = copy.deepcopy(DEFAULT_CONFIG)
            self._store.write(self._config)
            return self._config

        self._config = _deep_merge(DEFAULT_CONFIG, data)
        return self._config

    def get(self) -> dict[str, Any]:
        """Get the current configuration dictionary (live reference)."""
        return self._config

    def update(self, new_config: dict[str, Any]) -> None:
        """Replace and persist the configuration.

        The caller is responsible for shape validation. A deep copy is taken
        so subsequent caller-side mutation cannot corrupt persisted state.
        """
        self._config = copy.deepcopy(new_config)
        self._store.write(self._config)
        logger.info("Config updated and saved to %s", self.path)
