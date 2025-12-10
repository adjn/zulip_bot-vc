import logging
import os
from typing import Any, Dict

import yaml

logger = logging.getLogger(__name__)


class YAMLFileStore:
    def __init__(self, path: str) -> None:
        self.path = path

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def read(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except FileNotFoundError:
            return {}
        except Exception:
            logger.exception("Failed to read YAML file %s", self.path)
            return {}

    def write(self, data: Dict[str, Any]) -> None:
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False)
        os.replace(tmp_path, self.path)