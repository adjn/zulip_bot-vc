def normalize_phrase(s: str) -> str:
    """
    Normalize a phrase for strict-but-whitespace/case-insensitive matching.
    - strip leading/trailing whitespace
    - lower-case
    """
    return s.strip().lower()