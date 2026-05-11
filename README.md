# viofosync

Self-hosted web app for syncing, browsing, and exporting recordings from a Viofo dashcam (tested with the A229 Pro) over Wi-Fi. Runs as a single Docker container on a NAS or any always-on host on the same network as the dashcam.

> **v2 is a full rewrite.** v1 was a cron-driven CLI based on [BlackVueSync](https://github.com/acolomba/BlackVueSync). v2 uses the same dashcam protocol but ships a web UI, journey-detected GPS maps, ffmpeg exports, JSON-backed settings, a first-run setup wizard, and a UI-driven download manager. The v1 cron CLI is preserved on the `main` branch.

## Features

- **Archive browser** — view clips grouped by day, front/rear pairs, on-demand thumbnails, in-browser playback, kind filters (Driving / Parking / Read-only), GPS-maps toggle for low-bandwidth browsing.
- **GPS journeys** — Leaflet + OSM map per trip, automatic stop detection splits a day into journeys, reverse-geocoded start/end labels (e.g. *Whitegate → Sandiway*).
- **Exports** — select clip pairs, render joined front-only, rear-only, or picture-in-picture videos with ffmpeg. Hardware H.264 (videotoolbox / nvenc / qsv / vaapi) when available, software libx264 fallback.
- **Download manager** — live progress, reorderable queue, reachability badge, transient timeouts re-queue instead of burning retries.
- **Auto-delete from dashcam** *(optional)* — clears each clip from the device once it's downloaded and verified.
- **Settings page** — runtime settings hot-reload rather than Docker env vars; only `WEB_HOST`/`WEB_PORT` need a restart.

## Hardware

The dashcam must stay powered on and connected to Wi-Fi. A hardwire kit (e.g. Viofo HK4) plus a dedicated dashcam battery is recommended.

It should join your LAN in Wi-Fi **station** mode. As of May 2026 the official A229 Pro firmware does not retain Wi-Fi state across reboots but Viofo support will provide a custom firmware on request.

Reserve the dashcam's IP on your router so it doesn't change.

## Quick start

```bash
docker run -d \
  --name viofosync \
  -p 8080:8080 \
  -e PUID=$(id -u) \
  -e PGID=$(id -g) \
  -e TZ=Europe/London \
  -v /path/to/config:/config \
  -v /path/to/recordings:/recordings \
  --restart unless-stopped \
  robxyz/viofosync
```

Open `http://<host>:8080` and the first boot redirects you to a one-screen setup wizard at `/setup`. Enter the dashcam IP and an admin password (12+ characters) to finish. The wizard writes `/config/config.json` with a freshly-generated `SESSION_SECRET` and a bcrypt hash of the password — neither is held in env vars or the image.

After setup, every other setting lives on the **Settings** page in the UI.

> ⚠ **Setup window safety.** Until the wizard is submitted there is no auth on the container — the wizard self-disables after first submission and the route returns 404 thereafter. Don't expose the container to the public internet during this window.

## Configuration

The only Docker-level env vars are:


| Variable        | Description                                      | Default      |
| --------------- | ------------------------------------------------ | ------------ |
| `PUID` / `PGID` | Owner of `/config` and `/recordings` on the host | host UID/GID |
| `TZ`            | Timezone for log timestamps                      | UTC          |


App-level settings (sync interval, dashcam IP, encoder, geocoding email, web port, retention, password, auto-delete, etc.) are editable on the **Settings** page. Advanced users can hand-edit `/config/config.json` between restarts; the schema lives in `[web/settings_schema.py](web/settings_schema.py)`.

## Reverse geocoding

Journey and stop cards display their start/end as *"Street, Town"* via Nominatim (OpenStreetMap). Lookups are rate-limited to 1/second per [Nominatim's usage policy](https://operations.osmfoundation.org/policies/nominatim/) and cached in the `geocode_cache` table (coords rounded to 3 d.p., ≈110 m). Set **Nominatim email** in Settings → GPS & Geocoding to identify your install per OSM's terms; toggle the **GPS maps** filter off on the Archive page to skip the Leaflet + Nominatim machinery entirely for low-bandwidth browsing.

## XML vs HTML listing

By default the app scrapes the dashcam's HTML directory listings (`/DCIM/Movie`, `/DCIM/Movie/Parking`, `/DCIM/Movie/RO`), which is noticeably faster on some firmware than the XML API (`/?custom=1&cmd=3015&par=1`). Toggle off **Use HTML directory listing** in Settings → Dashcam to fall back to XML.

## Migrating from v1

Existing installs with a `viofosync.env` file are migrated automatically on first boot of the v2 image:

- Settings land in `/config/config.json`.
- The original `viofosync.env` is preserved with a deprecation header — safe to delete.
- The cron-style entry point is no longer the primary path; the web app's sync worker covers the same ground with live progress and queue control.

`PUID` / `PGID` / `TZ` env vars work the same as v1.

## Running without Docker

For development or for hosts that don't have Docker:

```bash
pip install -r requirements.txt
CONFIG_DIR=/path/to/config RECORDINGS=/path/to/archive \
  python3 -m web.launcher
```

`web.launcher` reads `WEB_HOST` / `WEB_PORT` from `config.json` (defaults `0.0.0.0:8080`) and re-execs into uvicorn. On first run, browse to `http://localhost:8080/setup`. `ffmpeg` must be on `$PATH` for thumbnails and exports.

## Credits

The GPX extraction logic uses the method described at [https://sergei.nz/extracting-gps-data-from-viofo-a119-and-other-novatek-powered-cameras/](https://sergei.nz/extracting-gps-data-from-viofo-a119-and-other-novatek-powered-cameras/).

This software is unaffiliated with Viofo or any other vendor.

## License

MIT — see [COPYING](COPYING).