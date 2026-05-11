from __future__ import annotations

from pathlib import Path

import bcrypt
import pytest

from web.settings import SettingsProvider, Snapshot


def _make(tmp_path: Path) -> SettingsProvider:
    return SettingsProvider(
        config_path=tmp_path / "config.json",
        env_file_path=tmp_path / "viofosync.env",
        recordings_dir=str(tmp_path / "rec"),
    )


def test_get_returns_snapshot_with_setup_mode_when_unconfigured(tmp_path: Path) -> None:
    provider = _make(tmp_path)
    snap = provider.get()
    assert isinstance(snap, Snapshot)
    assert snap.is_unconfigured is True
    assert snap.address is None


def test_update_persists_and_swaps_snapshot(tmp_path: Path) -> None:
    provider = _make(tmp_path)
    snap = provider.update({"ADDRESS": "1.2.3.4", "TIMEOUT": 15}, actor="test")
    assert snap.address == "1.2.3.4"
    assert snap.timeout == 15
    # Round-trip across a fresh provider.
    again = _make(tmp_path).get()
    assert again.address == "1.2.3.4"
    assert again.timeout == 15


def test_update_validates_input(tmp_path: Path) -> None:
    provider = _make(tmp_path)
    with pytest.raises(ValueError):
        provider.update({"TIMEOUT": -1}, actor="test")
    with pytest.raises(ValueError):
        provider.update({"NOPE": "x"}, actor="test")


def test_set_password_stores_bcrypt_hash(tmp_path: Path) -> None:
    provider = _make(tmp_path)
    provider.set_password("twelve-chars-min!", actor="test")
    snap = provider.get()
    assert snap.password_hash.startswith("$2")
    assert bcrypt.checkpw(b"twelve-chars-min!", snap.password_hash.encode())


def test_setting_password_drops_unconfigured_flag(tmp_path: Path) -> None:
    provider = _make(tmp_path)
    assert provider.get().is_unconfigured is True
    provider.set_password("twelve-chars-min!", actor="setup")
    assert provider.get().is_unconfigured is False


def test_rotate_session_secret_changes_value_and_persists(tmp_path: Path) -> None:
    provider = _make(tmp_path)
    provider.set_password("twelve-chars-min!", actor="setup")
    before = provider.get().session_secret
    after = provider.rotate_session_secret(actor="test")
    assert before != after
    assert _make(tmp_path).get().session_secret == after


def test_subscribe_fires_on_update(tmp_path: Path) -> None:
    provider = _make(tmp_path)
    provider.set_password("twelve-chars-min!", actor="setup")
    seen: list[tuple[str, ...]] = []
    provider.subscribe(lambda keys, snap: seen.append(tuple(sorted(keys))))
    provider.update({"TIMEOUT": 12}, actor="t")
    provider.update({"GROUPING": "weekly"}, actor="t")
    assert seen == [("TIMEOUT",), ("GROUPING",)]


def test_load_migrates_env_file_on_first_load(tmp_path: Path) -> None:
    env = tmp_path / "viofosync.env"
    env.write_text("ADDRESS=1.2.3.4\nWEB_PASSWORD=plaintext-twelve-chars\nTIMEOUT=20\n")
    provider = _make(tmp_path)
    snap = provider.get()
    assert snap.address == "1.2.3.4"
    assert snap.timeout == 20
    # WEB_PASSWORD plaintext was hashed during migration.
    assert snap.password_hash.startswith("$2")
    assert bcrypt.checkpw(b"plaintext-twelve-chars", snap.password_hash.encode())


def test_concurrent_updates_serialize(tmp_path: Path) -> None:
    """Two threads calling update() simultaneously don't tear the file."""
    import threading
    provider = _make(tmp_path)
    provider.set_password("twelve-chars-min!", actor="setup")

    errors: list[Exception] = []

    def writer(value: int) -> None:
        try:
            for _ in range(20):
                provider.update({"TIMEOUT": value}, actor=f"t{value}")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(v,)) for v in (5, 10, 15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    final = provider.get().timeout
    assert final in (5, 10, 15)


def test_delete_after_download_round_trips_through_snapshot(tmp_path: Path) -> None:
    provider = _make(tmp_path)
    snap = provider.update({"DELETE_AFTER_DOWNLOAD": True}, actor="test")
    assert snap.delete_after_download is True
    again = _make(tmp_path).get()
    assert again.delete_after_download is True


def test_delete_after_download_default_is_false(tmp_path: Path) -> None:
    snap = _make(tmp_path).get()
    assert snap.delete_after_download is False
