from __future__ import annotations

import json
from pathlib import Path

from web.launcher import resolve_bind


def test_resolve_bind_uses_defaults_when_no_config(tmp_path: Path) -> None:
    host, port = resolve_bind(tmp_path / "missing.json")
    assert host == "0.0.0.0"
    assert port == 8080


def test_resolve_bind_reads_from_config(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"WEB_HOST": "127.0.0.1", "WEB_PORT": 9000}))
    assert resolve_bind(cfg) == ("127.0.0.1", 9000)


def test_resolve_bind_falls_back_on_corrupt_json(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text("{not json")
    assert resolve_bind(cfg) == ("0.0.0.0", 8080)
