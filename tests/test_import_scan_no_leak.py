"""The import scan endpoint must not leak filenames of arbitrary
directories — the scan root can be any readable path the user types,
so returning every non-matching filename was an authenticated
directory-listing primitive. It reports counts only.
"""
from __future__ import annotations

from types import SimpleNamespace


def test_scan_does_not_leak_skipped_filenames(monkeypatch):
    from web.routers import imports as imports_router

    class _Item:
        def __init__(self, basename, size):
            self.basename = basename
            self.size_bytes = size

    class _Manifest:
        items = [_Item("2026_0101_080000_0001F.MP4", 10)]
        total_bytes = 10
        skipped = [
            {"name": "id_rsa", "reason": "not_recognised"},
            {"name": "secret-budget.xlsx", "reason": "not_recognised"},
        ]

    monkeypatch.setattr(imports_router.importer, "scan_source",
                        lambda root: _Manifest())
    monkeypatch.setattr(imports_router.importer, "present_in_archive",
                        lambda snap, sizes: set())
    monkeypatch.setattr(imports_router.importer, "is_cross_volume",
                        lambda a, b: False)
    monkeypatch.setattr(imports_router.importer, "scan_item_dict",
                        lambda it: {"basename": it.basename})
    monkeypatch.setattr(imports_router.os.path, "isdir", lambda p: True)

    snap = SimpleNamespace(import_path="/anything", recordings="/rec")
    monkeypatch.setattr(imports_router, "_snap", lambda req: snap)

    body = imports_router.scan(request=None, body=imports_router._PathBody(path="/etc"))

    blob = str(body)
    assert "id_rsa" not in blob and "secret-budget" not in blob, \
        "scan leaked arbitrary filenames"
    assert body["skipped_count"] == 2
    assert "skipped" not in body
