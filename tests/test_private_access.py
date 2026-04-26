"""Tests for `PrivateAccessFeature`, with a focus on the rule cache.

The feature parses `private_access.watch_rules` from config on every
incoming stream message. To keep the hot path cheap, parsed rules are
cached and keyed by `ConfigManager.version`; the cache invalidates when
config is replaced via `update()` (which is what the admin
`!access add/remove` commands call).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from config import ConfigManager
from features.private_access import PrivateAccessFeature
from tests.fakes import FakeClient


def _enabled_cm(tmp_path: Path) -> ConfigManager:
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    cm.load()
    cfg = cm.get()
    cfg["private_access"]["enabled"] = True
    cfg["private_access"]["watch_rules"] = [
        {
            "stream": "access-requests",
            "topic": "general",
            "phrase": "let me in",
            "target_stream": "secret-room",
        }
    ]
    cm.update(cfg)
    return cm


def test_load_rules_returns_parsed_rules(tmp_path: Path) -> None:
    cm = _enabled_cm(tmp_path)
    feat = PrivateAccessFeature(client=FakeClient(), config_mgr=cm)
    rules = feat._load_rules()
    assert len(rules) == 1
    assert rules[0].phrase == "let me in"
    assert rules[0].target_stream == "secret-room"


def test_disabled_returns_empty(tmp_path: Path) -> None:
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    cm.load()  # default: private_access.enabled = False
    feat = PrivateAccessFeature(client=FakeClient(), config_mgr=cm)
    assert feat._load_rules() == []


def test_cache_hits_when_version_unchanged(tmp_path: Path) -> None:
    cm = _enabled_cm(tmp_path)
    feat = PrivateAccessFeature(client=FakeClient(), config_mgr=cm)
    first = feat._load_rules()
    second = feat._load_rules()
    # Same list object — we returned the cached reference, didn't rebuild.
    assert first is second


def test_cache_invalidates_on_config_update(tmp_path: Path) -> None:
    cm = _enabled_cm(tmp_path)
    feat = PrivateAccessFeature(client=FakeClient(), config_mgr=cm)
    feat._load_rules()  # prime

    cfg = cm.get()
    cfg["private_access"]["watch_rules"].append(
        {
            "stream": "access-requests",
            "topic": "alpha",
            "phrase": "alpha please",
            "target_stream": "alpha-room",
        }
    )
    cm.update(cfg)

    rules = feat._load_rules()
    assert len(rules) == 2
    assert {r.phrase for r in rules} == {"let me in", "alpha please"}


def test_cache_invalidates_when_feature_disabled(tmp_path: Path) -> None:
    cm = _enabled_cm(tmp_path)
    feat = PrivateAccessFeature(client=FakeClient(), config_mgr=cm)
    assert feat._load_rules()  # primed and non-empty

    cfg = cm.get()
    cfg["private_access"]["enabled"] = False
    cm.update(cfg)

    assert feat._load_rules() == []


def test_invalid_rule_warns_once_per_version(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A malformed rule should log a warning once per config version, not
    once per call. This is the operator-visible payoff of caching: a busy
    stream with one broken rule shouldn't flood the log."""
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    cm.load()
    cfg = cm.get()
    cfg["private_access"]["enabled"] = True
    cfg["private_access"]["watch_rules"] = [
        {"stream": "x", "topic": "y", "phrase": "z"},  # missing target_stream
    ]
    cm.update(cfg)

    feat = PrivateAccessFeature(client=FakeClient(), config_mgr=cm)
    with caplog.at_level(logging.WARNING, logger="features.private_access"):
        for _ in range(5):
            feat._load_rules()

    invalid_warnings = [r for r in caplog.records if "Invalid watch rule" in r.message]
    assert len(invalid_warnings) == 1
