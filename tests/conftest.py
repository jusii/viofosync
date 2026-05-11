"""Shared pytest fixtures for the viofosync test suite."""
from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def tmp_config_dir(monkeypatch) -> Iterator[Path]:
    """Isolated /config directory for the duration of one test.

    Sets the CONFIG_DIR env var so code under test reads/writes
    inside the tempdir instead of /config on the host.
    """
    d = Path(tempfile.mkdtemp(prefix="viofosync-cfg-"))
    monkeypatch.setenv("CONFIG_DIR", str(d))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def tmp_recordings_dir(monkeypatch) -> Iterator[Path]:
    d = Path(tempfile.mkdtemp(prefix="viofosync-rec-"))
    monkeypatch.setenv("RECORDINGS", str(d))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)
