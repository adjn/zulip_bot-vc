"""File-based storage utilities for persisting bot data.

Provides a YAMLFileStore class for reading and writing YAML configuration files
with atomic write operations.
"""
import logging
import os
from typing import Any, Dict

import yaml

logger = logging.getLogger(__name__)


class YAMLFileStore:
    """Handles reading and writing YAML files with atomic operations.
    
    Attributes:
        path: Path to the YAML file
    """
    def __init__(self, path: str) -> None:
        self.path = path

    def exists(self) -> bool:
        """Check if the YAML file exists."""
        return os.path.exists(self.path)

    def read(self) -> Dict[str, Any]:
        """Read and parse the YAML file.
        
        Returns:
            Parsed YAML data as dictionary, or empty dict on error
        """
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except FileNotFoundError:
            return {}
        except Exception:  # pylint: disable=broad-exception-caught
            # Intentionally catch all exceptions to prevent YAML parsing errors
            # from crashing the bot
            logger.exception("Failed to read YAML file %s", self.path)
            return {}

    def write(self, data: Dict[str, Any]) -> None:
        """Write data to YAML file atomically.
        
        Uses a temporary file and atomic rename to prevent corruption.
        
        Args:
            data: Dictionary to write as YAML
        """
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False)
        os.replace(tmp_path, self.path)
