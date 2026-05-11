from __future__ import annotations

import bcrypt

from web.auth import Auth


def test_auth_accepts_pre_hashed_password() -> None:
    digest = bcrypt.hashpw(b"hunter2-twelve-chars", bcrypt.gensalt()).decode()
    auth = Auth(password_hash=digest, secret="x" * 64)
    assert auth.check_password("hunter2-twelve-chars") is True
    assert auth.check_password("wrong") is False


def test_auth_rotate_secret_invalidates_old_session_tokens() -> None:
    digest = bcrypt.hashpw(b"hunter2-twelve-chars", bcrypt.gensalt()).decode()
    auth = Auth(password_hash=digest, secret="a" * 64)
    from fastapi import Response
    resp = Response()
    old_token = auth.issue_session(resp)
    auth.rotate_secret("b" * 64)
    assert auth.validate_session(old_token) is False


def test_auth_update_password_hash_swaps_in_place() -> None:
    digest1 = bcrypt.hashpw(b"old-password-twelve", bcrypt.gensalt()).decode()
    digest2 = bcrypt.hashpw(b"new-password-twelve", bcrypt.gensalt()).decode()
    auth = Auth(password_hash=digest1, secret="x" * 64)
    auth.update_password_hash(digest2)
    assert auth.check_password("old-password-twelve") is False
    assert auth.check_password("new-password-twelve") is True
