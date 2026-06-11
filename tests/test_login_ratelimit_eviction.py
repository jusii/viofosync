"""The login rate-limiter must not accumulate stale per-IP buckets.

It keys on request.client.host; behind a reverse proxy that is one
shared IP (documented trade-off), but direct-LAN clients each get a
bucket — and buckets from IPs that never return used to linger
forever. Stale buckets are now swept.
"""
from __future__ import annotations

import web.auth as auth_mod
from web.auth import LOGIN_MAX_ATTEMPTS, Auth


def _auth() -> Auth:
    return Auth(password_hash="x", secret="s" * 32)


def test_rate_limit_still_blocks_within_window(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(auth_mod.time, "monotonic", lambda: now[0])
    a = _auth()
    for _ in range(LOGIN_MAX_ATTEMPTS):
        a.record_login_attempt("10.0.0.5")
    import pytest
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        a.record_login_attempt("10.0.0.5")


def test_stale_buckets_are_evicted(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(auth_mod.time, "monotonic", lambda: now[0])
    a = _auth()

    # 50 one-shot attempts from distinct IPs.
    for i in range(50):
        a.record_login_attempt(f"10.0.0.{i}")
    assert len(a._login_attempts) == 50

    # Time advances past the window; a single new attempt should sweep
    # every now-stale bucket rather than leaving them to grow forever.
    now[0] += auth_mod.LOGIN_WINDOW_SECONDS + 1
    a.record_login_attempt("10.0.1.1")

    assert len(a._login_attempts) == 1
    assert "10.0.1.1" in a._login_attempts
