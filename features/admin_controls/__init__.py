"""Admin DM commands.

Re-exports the public surface of :mod:`features.admin_controls.feature`
so existing ``from features.admin_controls import AdminControlsFeature``
imports keep working after the folder split.
"""

from features.admin_controls.feature import AdminControlsFeature, _redact

__all__ = ["AdminControlsFeature", "_redact"]
