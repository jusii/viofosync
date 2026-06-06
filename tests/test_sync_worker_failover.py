"""Active-address selection: primary first, alternative on fallback.

The selection helpers read only the settings snapshot and call
``_probe_one`` (a TCP probe). We construct a real SyncWorker with stub
provider/hub and monkeypatch ``_probe_one`` so no sockets are opened.
"""
from __future__ import annotations

import types

from web.services.sync_worker import SyncWorker


def _worker(address, fallback):
    snap = types.SimpleNamespace(address=address, address_fallback=fallback)
    provider = types.SimpleNamespace(get=lambda: snap)
    hub = types.SimpleNamespace()
    return SyncWorker(db=None, provider=provider, hub=hub)


def _patch_reachable(worker, reachable: set[str]):
    async def fake_probe_one(address: str) -> bool:
        return address in reachable
    worker._probe_one = fake_probe_one  # type: ignore[assignment]


async def test_primary_up_selects_primary() -> None:
    w = _worker("10.0.0.1", "10.0.0.2")
    _patch_reachable(w, {"10.0.0.1", "10.0.0.2"})
    assert await w._select_active_address() == ("10.0.0.1", "primary")


async def test_primary_down_alt_up_selects_alternative() -> None:
    w = _worker("10.0.0.1", "10.0.0.2")
    _patch_reachable(w, {"10.0.0.2"})
    assert await w._select_active_address() == ("10.0.0.2", "alternative")


async def test_both_down_is_offline() -> None:
    w = _worker("10.0.0.1", "10.0.0.2")
    _patch_reachable(w, set())
    assert await w._select_active_address() == (None, "offline")


async def test_no_fallback_configured_behaves_like_today() -> None:
    w = _worker("10.0.0.1", None)
    _patch_reachable(w, {"10.0.0.1"})
    assert await w._select_active_address() == ("10.0.0.1", "primary")
    _patch_reachable(w, set())
    assert await w._select_active_address() == (None, "offline")


async def test_empty_primary_uses_alternative() -> None:
    w = _worker(None, "10.0.0.2")
    _patch_reachable(w, {"10.0.0.2"})
    assert await w._select_active_address() == ("10.0.0.2", "alternative")


def test_fetch_listing_uses_active_address(monkeypatch) -> None:
    import web.services.sync_worker as sw

    snap = types.SimpleNamespace(
        address="10.0.0.1", address_fallback="10.0.0.2",
        use_html_listing=False,
    )
    provider = types.SimpleNamespace(get=lambda: snap)
    worker = sw.SyncWorker(db=None, provider=provider,
                           hub=types.SimpleNamespace())
    worker._active_address = "10.0.0.2"   # pretend the alternative won

    seen = {}

    def fake_xml(base):
        seen["base"] = base
        return []

    monkeypatch.setattr(sw.vfs, "get_dashcam_filenames", fake_xml)
    worker._fetch_listing()
    assert seen["base"] == "http://10.0.0.2"
