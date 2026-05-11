from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client_unconfigured(tmp_config_dir: Path, tmp_recordings_dir: Path):
    from web import app as app_mod
    from web import settings as settings_mod
    settings_mod.reset_for_tests()
    application = app_mod.create_app()
    with TestClient(application) as c:
        yield c


def test_unconfigured_redirects_to_setup(client_unconfigured) -> None:
    r = client_unconfigured.get("/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"].endswith("/setup")


def test_static_assets_pass_through_during_setup(client_unconfigured) -> None:
    r = client_unconfigured.get("/static/styles.css", follow_redirects=False)
    # 200 if the file exists; 404 from the static handler is fine — we
    # only care it isn't a 307 to /setup.
    assert r.status_code != 307


def test_setup_route_itself_passes_through(client_unconfigured) -> None:
    r = client_unconfigured.get("/setup", follow_redirects=False)
    # Until Task 11 wires the route this returns 404, NOT 307.
    assert r.status_code != 307


def test_get_setup_returns_html(client_unconfigured) -> None:
    r = client_unconfigured.get("/setup")
    assert r.status_code == 200
    assert "Set up Viofosync" in r.text


def test_post_setup_completes_configuration(client_unconfigured) -> None:
    r = client_unconfigured.post("/setup", data={
        "address": "192.168.1.230",
        "password": "twelve-chars-min!",
        "confirm": "twelve-chars-min!",
    }, follow_redirects=False)
    assert r.status_code == 303
    # After completion, GET / no longer redirects to /setup.
    r = client_unconfigured.get("/", follow_redirects=False)
    assert r.status_code in (200, 401)


def test_post_setup_rejects_short_password(client_unconfigured) -> None:
    r = client_unconfigured.post("/setup", data={
        "address": "192.168.1.230",
        "password": "short",
        "confirm": "short",
    })
    assert r.status_code == 400
    assert "8" in r.text


def test_post_setup_rejects_mismatched_confirm(client_unconfigured) -> None:
    r = client_unconfigured.post("/setup", data={
        "address": "192.168.1.230",
        "password": "twelve-chars-min!",
        "confirm": "different-pw-twelve!",
    })
    assert r.status_code == 400
    assert "match" in r.text.lower()


def test_setup_routes_404_after_completion(client_unconfigured) -> None:
    client_unconfigured.post("/setup", data={
        "address": "192.168.1.230",
        "password": "twelve-chars-min!",
        "confirm": "twelve-chars-min!",
    })
    r = client_unconfigured.get("/setup")
    assert r.status_code == 404


def test_test_dashcam_during_setup(client_unconfigured) -> None:
    # Probes 127.0.0.1:1 — guaranteed to fail; we just need a structured response.
    r = client_unconfigured.post(
        "/api/setup/test-dashcam",
        json={"address": "127.0.0.1:1"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "ok" in body and "error" in body
