"""Atomic JSON read/write for /config/config.json.

Writes go through a temp file + ``os.replace`` for atomicity, so a
crash mid-write leaves the previous version intact rather than a
truncated file. The store does not interpret values — it just
serialises the dict it's handed. Validation lives in
``web.settings_schema``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from enum import StrEnum
from pathlib import Path

log = logging.getLogger("viofosync.config")


class MigrationResult(StrEnum):
    MIGRATED = "migrated"
    SKIPPED_ALREADY_MIGRATED = "skipped_already_migrated"
    SKIPPED_NO_SOURCE = "skipped_no_source"


class ConfigStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    # ------------------------------------------------------------------ load

    def load(self) -> dict:
        """Return the parsed JSON dict, or {} if missing/corrupt.

        Corrupt files log a warning and return {} so the app can
        boot into setup mode rather than crash on a tampered file.
        """
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except json.JSONDecodeError as e:
            log.warning("config.json is corrupt — using empty config: %s", e)
            return {}
        if not isinstance(data, dict):
            raise ValueError("config.json must be a JSON object at the top level")
        return data

    # ----------------------------------------------------------------- write

    def write(self, data: dict) -> None:
        """Atomically replace the config file with the given dict."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")

        # NamedTemporaryFile in the same directory so os.replace is atomic
        # (rename across filesystems is not).
        fd, tmp = tempfile.mkstemp(
            prefix=".config-", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except Exception:
            # Best-effort cleanup; original file is untouched.
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

        # fsync the directory so the rename itself is durable.
        try:
            dir_fd = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # Some filesystems (tmpfs, SMB) don't support directory fsync.
            pass

    # -------------------------------------------------------------- migrate

    _ENV_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
    _BOOL_KEYS = {
        "HTML", "GPS_EXTRACT", "ENABLE_SCHEDULED_SYNC", "GEOCODE_ENABLED",
    }
    _INT_KEYS = {
        "TIMEOUT", "DOWNLOAD_ATTEMPTS", "MAX_DOWNLOAD_ATTEMPTS",
        "SYNC_INTERVAL", "WEB_PORT",
    }

    def migrate_from_env(self, env_file: Path | str) -> MigrationResult:
        """Parse ``env_file`` and write its values to ``self.path``.

        Runs at most once: if ``self.path`` already exists, this is a
        no-op. After a successful migration the source env file is
        rewritten with a header comment marking it deprecated.
        """
        env_path = Path(env_file)
        if self.path.exists():
            return MigrationResult.SKIPPED_ALREADY_MIGRATED
        if not env_path.exists():
            return MigrationResult.SKIPPED_NO_SOURCE

        out: dict = {}
        for raw in env_path.read_text("utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = self._ENV_RE.match(line)
            if not m:
                log.warning("ignoring malformed line in %s: %r", env_path, line)
                continue
            key, val = m.group(1), m.group(2)
            # Strip surrounding quotes (single or double) but preserve
            # interior whitespace.
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                val = val[1:-1]
            out[key] = self._coerce(key, val)

        self.write(out)

        env_path.write_text(
            "# This file is no longer used. Configuration now lives in\n"
            "# /config/config.json and is edited via the web UI.\n"
            "# Kept here as a one-shot rollback path; safe to delete.\n"
            "#\n" + env_path.read_text("utf-8"),
            encoding="utf-8",
        )
        log.info("migrated %s -> %s", env_path, self.path)
        return MigrationResult.MIGRATED

    @classmethod
    def _coerce(cls, key: str, raw: str) -> object:
        if key in cls._BOOL_KEYS:
            return raw.strip().lower() in ("1", "true", "yes", "on")
        if key in cls._INT_KEYS:
            try:
                return int(raw.strip())
            except ValueError:
                return raw  # let validation reject it later
        return raw
