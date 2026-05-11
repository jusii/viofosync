"""The SPA's index.html is rewritten on the way out so static
asset URLs carry an mtime stamp — without it the browser sticks
to an old app.js / styles.css across releases and the user sees
half-updated UI for hours."""
from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi.testclient import TestClient


def test_index_rewrites_static_urls_with_mtime(
    tmp_config_dir: Path, tmp_recordings_dir: Path,
) -> None:
    from web import app as app_mod
    from web import settings as settings_mod
    settings_mod.reset_for_tests()
    app = app_mod.create_app()

    with TestClient(app) as c:
        # Index is served outside setup-mode redirects only when
        # the wizard has been completed; do that first.
        c.post("/setup", data={
            "address": "192.168.1.230",
            "password": "twelve-chars-min!",
            "confirm": "twelve-chars-min!",
        }, follow_redirects=False)

        r = c.get("/")
        assert r.status_code == 200
        html = r.text

        # Asset references should now carry a numeric ?v= stamp.
        assert re.search(
            r'/static/app\.js\?v=\d+', html
        ), "app.js URL is missing the cache-bust stamp"
        assert re.search(
            r'/static/styles\.css\?v=\d+', html
        ), "styles.css URL is missing the cache-bust stamp"

        # And the HTML itself should be served no-cache so the
        # rewritten URLs always reach the browser.
        cc = r.headers.get("cache-control", "")
        assert "no-cache" in cc.lower(), (
            f"Expected no-cache header on /, got {cc!r}"
        )


def test_index_stamps_track_file_mtime(
    tmp_config_dir: Path, tmp_recordings_dir: Path,
) -> None:
    """When app.js is touched the served URL stamp should
    change — that's how the browser knows to refetch."""
    from web import app as app_mod
    from web import settings as settings_mod
    settings_mod.reset_for_tests()
    app = app_mod.create_app()

    static_dir = Path(app_mod.STATIC_DIR)
    app_js = static_dir / "app.js"

    with TestClient(app) as c:
        c.post("/setup", data={
            "address": "192.168.1.230",
            "password": "twelve-chars-min!",
            "confirm": "twelve-chars-min!",
        }, follow_redirects=False)

        r1 = c.get("/")
        m1 = re.search(r'/static/app\.js\?v=(\d+)', r1.text)
        assert m1, "first response missing app.js cache-bust"

        # Bump the file's mtime forward and refetch.
        st = app_js.stat()
        os.utime(app_js, (st.st_atime, st.st_mtime + 60))

        r2 = c.get("/")
        m2 = re.search(r'/static/app\.js\?v=(\d+)', r2.text)
        assert m2, "second response missing app.js cache-bust"
        assert m1.group(1) != m2.group(1), (
            "cache-bust stamp didn't change after mtime touch"
        )

        # Restore so the test doesn't surprise other runs.
        os.utime(app_js, (st.st_atime, st.st_mtime))
