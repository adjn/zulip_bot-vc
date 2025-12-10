"""Text matching utilities for comparing phrases.

Provides normalization functions for case-insensitive and whitespace-tolerant
string comparisons.
"""


def normalize_phrase(s: str) -> str:
    """
    Normalize a phrase for strict-but-whitespace/case-insensitive matching.
    - strip leading/trailing whitespace
    - lower-case
    """
    return s.strip().lower()
