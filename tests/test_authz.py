"""Tests for :mod:`core.authz`.

Covers:

* Role resolution for public / admin / owner / super-admin.
* Allowlist precedence over platform role (the "compromised owner"
  defense).
* TTL caching: hit, expiry, and explicit invalidation.
* ``require()`` comparison semantics.
"""

from __future__ import annotations

import time

import pytest

from config import ConfigManager
from core.authz import Authorizer, Role
from tests.fakes import FakeClient


def _cm(tmp_path, **admin_overrides):
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    cm.load()
    cfg = cm.get()
    cfg.setdefault("admin", {}).update(admin_overrides)
    return cm


@pytest.mark.trio
async def test_public_user_default(tmp_path) -> None:
    fc = FakeClient()
    fc.users[1] = {"is_admin": False, "is_owner": False}
    az = Authorizer(client=fc, config_mgr=_cm(tmp_path))
    assert await az.role_for(1) == Role.public


@pytest.mark.trio
async def test_platform_admin_resolves_to_admin(tmp_path) -> None:
    fc = FakeClient()
    fc.users[1] = {"is_admin": True, "is_owner": False}
    az = Authorizer(client=fc, config_mgr=_cm(tmp_path))
    assert await az.role_for(1) == Role.admin


@pytest.mark.trio
async def test_platform_owner_resolves_to_admin(tmp_path) -> None:
    fc = FakeClient()
    fc.users[1] = {"is_admin": False, "is_owner": True}
    az = Authorizer(client=fc, config_mgr=_cm(tmp_path))
    assert await az.role_for(1) == Role.admin


@pytest.mark.trio
async def test_super_admin_allowlist_independent_of_platform(tmp_path) -> None:
    # User is *not* a Zulip admin, but is on the super-admin allowlist:
    # they must still resolve to super_admin. This is the rd-opus
    # "even if the org owner is compromised" defense.
    fc = FakeClient()
    fc.users[7] = {"is_admin": False, "is_owner": False}
    az = Authorizer(client=fc, config_mgr=_cm(tmp_path, super_admin_user_ids=[7]))
    assert await az.role_for(7) == Role.super_admin


@pytest.mark.trio
async def test_platform_admin_not_in_list_keeps_admin_role(tmp_path) -> None:
    # super_admin_user_ids being non-empty must NOT demote regular admins.
    fc = FakeClient()
    fc.users[1] = {"is_admin": True, "is_owner": False}
    fc.users[7] = {"is_admin": False, "is_owner": False}
    az = Authorizer(client=fc, config_mgr=_cm(tmp_path, super_admin_user_ids=[7]))
    assert await az.role_for(1) == Role.admin
    assert await az.role_for(7) == Role.super_admin


@pytest.mark.trio
async def test_role_cache_hits_within_ttl(tmp_path) -> None:
    fc = FakeClient()
    fc.users[1] = {"is_admin": True, "is_owner": False}
    az = Authorizer(client=fc, config_mgr=_cm(tmp_path, role_cache_ttl_seconds=300))
    assert await az.role_for(1) == Role.admin
    # Flip the underlying user; cache should still report admin.
    fc.users[1] = {"is_admin": False, "is_owner": False}
    assert await az.role_for(1) == Role.admin


@pytest.mark.trio
async def test_role_cache_invalidate(tmp_path) -> None:
    fc = FakeClient()
    fc.users[1] = {"is_admin": True, "is_owner": False}
    az = Authorizer(client=fc, config_mgr=_cm(tmp_path, role_cache_ttl_seconds=300))
    assert await az.role_for(1) == Role.admin
    fc.users[1] = {"is_admin": False, "is_owner": False}
    az.invalidate(1)
    assert await az.role_for(1) == Role.public


@pytest.mark.trio
async def test_role_cache_expires(tmp_path, monkeypatch) -> None:
    fc = FakeClient()
    fc.users[1] = {"is_admin": True, "is_owner": False}
    az = Authorizer(client=fc, config_mgr=_cm(tmp_path, role_cache_ttl_seconds=1))
    base = time.monotonic()
    monkeypatch.setattr("core.authz.time.monotonic", lambda: base)
    assert await az.role_for(1) == Role.admin
    # Move clock forward past the TTL; underlying state has changed.
    monkeypatch.setattr("core.authz.time.monotonic", lambda: base + 5)
    fc.users[1] = {"is_admin": False, "is_owner": False}
    assert await az.role_for(1) == Role.public


@pytest.mark.trio
async def test_require_compares_against_min_role(tmp_path) -> None:
    fc = FakeClient()
    fc.users[1] = {"is_admin": True, "is_owner": False}
    fc.users[7] = {"is_admin": False, "is_owner": False}
    az = Authorizer(client=fc, config_mgr=_cm(tmp_path, super_admin_user_ids=[7]))
    # Admin clears Role.admin but not Role.super_admin.
    assert await az.require(1, Role.admin) is True
    assert await az.require(1, Role.super_admin) is False
    # Super clears every level.
    assert await az.require(7, Role.public) is True
    assert await az.require(7, Role.admin) is True
    assert await az.require(7, Role.super_admin) is True


@pytest.mark.trio
async def test_malformed_super_admin_entries_ignored(tmp_path) -> None:
    fc = FakeClient()
    fc.users[1] = {"is_admin": False, "is_owner": False}
    az = Authorizer(
        client=fc,
        config_mgr=_cm(tmp_path, super_admin_user_ids=["bogus", None, 1]),
    )
    # "bogus" and None are dropped; 1 still grants super.
    assert await az.role_for(1) == Role.super_admin
