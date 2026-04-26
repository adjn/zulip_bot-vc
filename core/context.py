"""Shared dependency container for feature handlers.

Every :class:`core.dispatcher.FeatureHandler` receives a
:class:`FeatureContext` at construction time. The container holds the
references that more than one feature needs (Zulip client, config
manager, durable storage, deletion scheduler, bot identity).

Why this exists
---------------

Without a shared container, adding a new cross-cutting resource (an
audit-log facade, an authorization helper, a rate limiter, …) means
changing every feature's ``__init__`` signature *and* every test that
constructs a feature. With ``FeatureContext`` the change is a single
new field here plus a constructor argument in :mod:`bot_main`. Tests
extend the same way.

Optional fields
---------------

``storage`` and ``scheduler`` are typed as optional because not every
feature needs them (``private_access`` reads only the live config).
Features that *do* require them expose narrowed properties that assert
non-``None`` access; production constructs the context with all fields
populated, so the assertion never fires there. Tests for features that
don't need a field may leave it ``None``.

Frozen
------

The dataclass is frozen so a feature can't accidentally swap another
feature's dependencies (e.g. by reassigning ``self.ctx.client``). Any
mutation has to go through reconstructing the context, which only
happens at startup.
"""

from __future__ import annotations

from dataclasses import dataclass

from config import ConfigManager
from core.audit import AuditLog
from core.authz import Authorizer
from core.client import ClientProtocol
from storage.db import Storage
from utils.scheduling import DeletionScheduler


@dataclass(frozen=True)
class FeatureContext:
    """Shared resources injected into every :class:`FeatureHandler`."""

    client: ClientProtocol
    config_mgr: ConfigManager
    storage: Storage | None = None
    scheduler: DeletionScheduler | None = None
    authz: Authorizer | None = None
    audit: AuditLog | None = None
    bot_user_id: int | None = None
