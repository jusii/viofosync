"""Pre-uvicorn launcher.

Reads ``WEB_HOST``/``WEB_PORT`` from /config/config.json so the
container entrypoint doesn't need to source an env file. Defaults to
0.0.0.0:8080 if the config is missing/corrupt — keeps the first-run
wizard reachable on the standard port for fresh installs.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080


def resolve_bind(config_path: Path | str) -> tuple[str, int]:
    p = Path(config_path)
    if not p.exists():
        return DEFAULT_HOST, DEFAULT_PORT
    try:
        data = json.loads(p.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return DEFAULT_HOST, DEFAULT_PORT
    host = data.get("WEB_HOST", DEFAULT_HOST) or DEFAULT_HOST
    try:
        port = int(data.get("WEB_PORT", DEFAULT_PORT))
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    if not (1 <= port <= 65535):
        port = DEFAULT_PORT
    return str(host), port


def main() -> None:
    config_dir = Path(os.environ.get("CONFIG_DIR", "/config"))
    host, port = resolve_bind(config_dir / "config.json")
    # Re-exec into uvicorn so it owns PID 1 (the entrypoint
    # already exec'd into us via su-exec). sys.executable
    # preserves the current interpreter — using `python3` would
    # find the system Python and miss any venv's site-packages.
    os.execv(sys.executable, [
        sys.executable, "-m", "uvicorn",
        "web.app:create_app", "--factory",
        "--host", host, "--port", str(port),
    ])


if __name__ == "__main__":
    main()
