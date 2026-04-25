"""Text matching utilities."""

from __future__ import annotations


def normalize_phrase(s: str) -> str:
    """Normalize a phrase for whitespace/case-insensitive equality matching."""
    return s.strip().lower()
