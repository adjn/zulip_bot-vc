"""File-based storage utilities for persisting bot data."""

from __future__ import annotations

import logging
import os
from typing import Any

import yaml

logger = logging.getLogger(__name__)


class YAMLFileStore:
    """Reads / writes a YAML file with atomic writes (tmp + rename)."""

    def __init__(self, path: str) -> None:
        self.path = path

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def read(self) -> dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except yaml.YAMLError:
            logger.exception("Failed to parse YAML file %s", self.path)
            return {}
        except OSError:
            logger.exception("Failed to read YAML file %s", self.path)
            return {}

    def write(self, data: dict[str, Any]) -> None:
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False)
        os.replace(tmp_path, self.path)
