"""Tests pinning the `FeatureContext` contract.

Cheap tests that fail loudly if someone removes a required field, makes
a frozen field mutable, or changes default values for the optional
fields. The features themselves are exercised end-to-end elsewhere; this
file is the type-and-shape gate.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from config import ConfigManager
from core.context import FeatureContext
from tests.fakes import FakeClient


def _cm(tmp_path: Path) -> ConfigManager:
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    cm.load()
    return cm


def test_minimal_context_only_requires_client_and_config(tmp_path: Path) -> None:
    """`storage`, `scheduler`, `bot_user_id` all default to None."""
    ctx = FeatureContext(client=FakeClient(), config_mgr=_cm(tmp_path))
    assert ctx.storage is None
    assert ctx.scheduler is None
    assert ctx.bot_user_id is None


def test_context_is_frozen(tmp_path: Path) -> None:
    """Frozen so a feature can't accidentally swap shared deps."""
    ctx = FeatureContext(client=FakeClient(), config_mgr=_cm(tmp_path))
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.bot_user_id = 42  # type: ignore[misc]


def test_context_carries_bot_user_id(tmp_path: Path) -> None:
    ctx = FeatureContext(client=FakeClient(), config_mgr=_cm(tmp_path), bot_user_id=99)
    assert ctx.bot_user_id == 99
