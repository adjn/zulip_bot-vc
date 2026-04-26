"""Tiered authorization model.

Today every privileged command in :mod:`features.admin_controls` is
gated on Zulip's ``is_admin`` / ``is_owner`` flags. That's a single
boolean cliff: anyone who wins admin can do anything the bot supports.

This module introduces a small ordered :class:`Role` enum and an
:class:`Authorizer` that resolves a user id to a role. Future
commands can opt into a stricter level (e.g. ``Role.super_admin`` for
secret-touching ops) without each feature reinventing role lookup.

Roles
-----

* ``Role.public`` — everyone.
* ``Role.admin`` — Zulip ``is_admin`` or ``is_owner``.
* ``Role.super_admin`` — explicit allowlist in
  ``config.admin.super_admin_user_ids``. Independent of platform role
  on purpose: it's the "defense even if the org owner is compromised"
  knob that the rd-opus review called out. Today no command requires
  super; the level exists so the next sensitive feature can opt in.

(``Role.mod`` is intentionally absent — there's no mod source today.
Add it the day there's a config or platform signal that distinguishes
mods from admins, so the values stay meaningful.)

Caching
-------

Role lookup hits ``client.get_user_by_id`` which is a Zulip API call.
The :class:`Authorizer` caches per-user with a configurable TTL
(``admin.role_cache_ttl_seconds``, default 60s). Use
:meth:`Authorizer.invalidate` to drop a cached entry — handy on
admin-list changes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntEnum

from config import ConfigManager
from core.client import ClientProtocol


class Role(IntEnum):
    """Ordered role levels. Higher values include lower privileges."""

    public = 0
    admin = 10
    super_admin = 20


@dataclass
class Authorizer:
    """Resolves Zulip user ids to :class:`Role` levels with a TTL cache."""

    client: ClientProtocol
    config_mgr: ConfigManager
    _cache: dict[int, tuple[Role, float]] = field(default_factory=dict, init=False, repr=False)

    def _admin_cfg(self) -> dict:
        return self.config_mgr.get().get("admin", {})

    def _ttl(self) -> float:
        return float(self._admin_cfg().get("role_cache_ttl_seconds", 60))

    def _super_ids(self) -> set[int]:
        raw = self._admin_cfg().get("super_admin_user_ids", []) or []
        out: set[int] = set()
        for x in raw:
            try:
                out.add(int(x))
            except (TypeError, ValueError):
                # Ignore malformed entries silently per call; the operator
                # gets a clearer signal at config-load time if we ever wire
                # schema validation in.
                continue
        return out

    async def role_for(self, user_id: int) -> Role:
        """Return the user's current :class:`Role`, hitting the cache first."""
        cached = self._cache.get(user_id)
        if cached is not None:
            role, expires_at = cached
            if time.monotonic() < expires_at:
                return role
        role = await self._lookup_role(user_id)
        self._cache[user_id] = (role, time.monotonic() + self._ttl())
        return role

    async def _lookup_role(self, user_id: int) -> Role:
        # Super-admin first: an explicit allowlist takes precedence over
        # platform role so a compromised org-admin can still be denied.
        if user_id in self._super_ids():
            return Role.super_admin
        user = await self.client.get_user_by_id(user_id)
        if user and (user.get("is_admin") or user.get("is_owner")):
            return Role.admin
        return Role.public

    async def require(self, user_id: int, min_role: Role) -> bool:
        """Return True iff the user's role is at least ``min_role``."""
        return await self.role_for(user_id) >= min_role

    def invalidate(self, user_id: int | None = None) -> None:
        """Drop a cached role (or all, if ``user_id`` is ``None``)."""
        if user_id is None:
            self._cache.clear()
        else:
            self._cache.pop(user_id, None)
