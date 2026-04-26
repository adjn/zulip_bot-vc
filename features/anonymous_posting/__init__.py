"""Anonymous posting feature.

Re-exports the public surface of :mod:`features.anonymous_posting.feature`
so existing imports keep working after the folder split.
"""

from features.anonymous_posting.feature import (
    AnonymousPostingFeature,
    _escape_for_code_fence,
    _scrub_wildcards,
)

__all__ = [
    "AnonymousPostingFeature",
    "_escape_for_code_fence",
    "_scrub_wildcards",
]
