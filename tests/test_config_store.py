"""ConfigStore: atomic JSON read/write + migration shim."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from web.config_store import ConfigStore, MigrationResult


def test_load_returns_empty_dict_when_file_missing(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    assert store.load() == {}


def test_write_persists_and_loads_back(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.write({"ADDRESS": "1.2.3.4", "TIMEOUT": 15})
    assert store.load() == {"ADDRESS": "1.2.3.4", "TIMEOUT": 15}


def test_write_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If os.replace is interrupted, the original file is intact."""
    store = ConfigStore(tmp_path / "config.json")
    store.write({"ADDRESS": "1.1.1.1"})

    real_replace = os.replace

    def boom(src: str, dst: str) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.write({"ADDRESS": "2.2.2.2"})

    monkeypatch.setattr(os, "replace", real_replace)
    assert store.load() == {"ADDRESS": "1.1.1.1"}


def test_write_sets_mode_0600(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    store = ConfigStore(cfg)
    store.write({"FOO": "bar"})
    assert (cfg.stat().st_mode & 0o777) == 0o600


def test_load_rejects_non_object_top_level(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text("[1,2,3]")
    store = ConfigStore(cfg)
    with pytest.raises(ValueError, match="must be a JSON object"):
        store.load()


def test_load_returns_empty_on_corrupt_json(tmp_path: Path, caplog) -> None:
    """A corrupt config.json shouldn't brick the app — log + return {}."""
    cfg = tmp_path / "config.json"
    cfg.write_text("{not json")
    store = ConfigStore(cfg)
    assert store.load() == {}
    assert "corrupt" in caplog.text.lower()


def test_migrate_from_env_file_parses_kv_pairs(tmp_path: Path) -> None:
    env = tmp_path / "viofosync.env"
    env.write_text(
        "# header comment\n"
        "ADDRESS=192.168.1.230\n"
        'WEB_PASSWORD="secret with spaces"\n'
        "TIMEOUT=15\n"
        "\n"
        "GPS_EXTRACT=1\n"
    )
    cfg = tmp_path / "config.json"
    store = ConfigStore(cfg)
    result = store.migrate_from_env(env)

    assert result == MigrationResult.MIGRATED
    data = json.loads(cfg.read_text())
    assert data["ADDRESS"] == "192.168.1.230"
    assert data["WEB_PASSWORD"] == "secret with spaces"
    assert data["TIMEOUT"] == 15
    assert data["GPS_EXTRACT"] is True
    # Original env file is preserved with a header comment.
    assert "no longer used" in env.read_text().lower()


def test_migrate_skipped_when_config_already_exists(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text('{"ADDRESS": "old"}')
    env = tmp_path / "viofosync.env"
    env.write_text("ADDRESS=new\n")

    store = ConfigStore(cfg)
    assert store.migrate_from_env(env) == MigrationResult.SKIPPED_ALREADY_MIGRATED
    assert json.loads(cfg.read_text()) == {"ADDRESS": "old"}


def test_migrate_skipped_when_env_file_missing(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    store = ConfigStore(cfg)
    assert store.migrate_from_env(tmp_path / "missing.env") == MigrationResult.SKIPPED_NO_SOURCE
    assert not cfg.exists()
