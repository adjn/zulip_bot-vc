"""Private access (low-friction self-subscribe) feature.

Re-exports the public surface of :mod:`features.private_access.feature`
so existing imports keep working after the folder split.
"""

from features.private_access.feature import PrivateAccessFeature

__all__ = ["PrivateAccessFeature"]
