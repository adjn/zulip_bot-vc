from pathlib import Path

import yaml

from config import DEFAULT_CONFIG, ConfigManager, _deep_merge


def test_default_creates_file(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    cm = ConfigManager(str(p))
    cfg = cm.load()
    assert p.exists()
    assert cfg["anonymous_posting"]["enabled"] is False
    assert cfg["private_access"]["enabled"] is False


def test_default_config_not_mutated_across_loads(tmp_path: Path) -> None:
    p1 = tmp_path / "a.yaml"
    p2 = tmp_path / "b.yaml"
    a = ConfigManager(str(p1)).load()
    a["anonymous_posting"]["target_stream"] = "mutated"
    b = ConfigManager(str(p2)).load()
    assert b["anonymous_posting"]["target_stream"] == "anonymous"
    # And DEFAULT_CONFIG itself is untouched
    assert DEFAULT_CONFIG["anonymous_posting"]["target_stream"] == "anonymous"


def test_deep_merge_preserves_unspecified_nested_keys() -> None:
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    overlay = {"a": {"y": 20}}
    out = _deep_merge(base, overlay)
    assert out == {"a": {"x": 1, "y": 20}, "b": 3}
    # base unchanged
    assert base == {"a": {"x": 1, "y": 2}, "b": 3}


def test_load_merges_user_overrides(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "anonymous_posting": {
                    "enabled": True,
                    "target_stream": "secret",
                }
            }
        )
    )
    cm = ConfigManager(str(p))
    cfg = cm.load()
    # Overrides applied
    assert cfg["anonymous_posting"]["enabled"] is True
    assert cfg["anonymous_posting"]["target_stream"] == "secret"
    # Defaults still present
    assert cfg["anonymous_posting"]["target_topic"] == "general"
    assert cfg["private_access"]["enabled"] is False


def test_malformed_yaml_resets_to_defaults(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("this is: : not yaml: [")
    cm = ConfigManager(str(p))
    cfg = cm.load()
    assert cfg["anonymous_posting"]["enabled"] is False


def test_update_persists_atomically(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    cm = ConfigManager(str(p))
    cm.load()
    new = cm.get()
    new["anonymous_posting"]["enabled"] = True
    cm.update(new)

    cm2 = ConfigManager(str(p))
    cfg2 = cm2.load()
    assert cfg2["anonymous_posting"]["enabled"] is True


def test_version_starts_at_zero_before_load(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    cm = ConfigManager(str(p))
    assert cm.version == 0


def test_version_bumps_on_load_and_update(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    cm = ConfigManager(str(p))
    cm.load()
    after_load = cm.version
    assert after_load > 0

    new = cm.get()
    new["anonymous_posting"]["enabled"] = True
    cm.update(new)
    assert cm.version == after_load + 1


def test_version_bumps_on_each_reload(tmp_path: Path) -> None:
    """Re-calling load() should bump the version even when the on-disk
    file is unchanged. This keeps the contract simple: 'version changed'
    means 'caches must rebuild', without trying to detect no-op reloads.
    """
    p = tmp_path / "config.yaml"
    cm = ConfigManager(str(p))
    cm.load()
    v1 = cm.version
    cm.load()
    assert cm.version == v1 + 1
