# Changelog

## Unreleased

### Added
- Optional **Alternative address** for the same camera (Settings →
  Dashcam, setting `ADDRESS_FALLBACK`). The primary is always tried
  first; the alternative is used only when the primary is unreachable,
  and sync returns to the primary automatically. Intended for reaching
  one dashcam over a VPN (e.g. a Raspberry Pi hotspot, or a site-to-site
  VPN to a second parking location) — not for a second camera. A new
  Home Assistant `dashcam_connection` sensor reports `primary` /
  `alternative` / `offline` with the live address as an attribute, and
  the web UI shows a "via alternative" chip while on the fallback.
- MQTT publishing with Home Assistant auto-discovery. New Settings
  panel exposes broker host/port/credentials, TLS, topic prefix, and
  discovery prefix. Publishes 12 sensor/binary_sensor entities and 6
  action buttons; idle traffic is zero thanks to per-topic change
  detection and coalescing. LWT keeps HA's view consistent with
  viofosync's actual state.
- `sync_status` now reports `error` for sticky problems: missing
  `ADDRESS` configuration, recordings path not writable, camera
  authentication failure (HTTP 401/403), or disk usage at/above
  `DISK_CRITICAL_PCT` (new setting, default 95%).
- The HA `sync_status` sensor exposes a `reason` JSON attribute
  populated when state is `error`. Surface it in Lovelace with
  `state_attr('sensor.viofosync_sync_status', 'reason')`.
- New `DISK_CRITICAL_PCT` setting (Snapshot field `disk_critical_pct`)
  configures the disk-pressure threshold above which sync goes into
  `error`. Must be `>= RETENTION_DISK_PCT`.
- The download manager now shows a session-wide moving-average download
  speed and an estimated time to complete while a sync is running.
- New Home Assistant `download_speed` sensor (`data_rate`, MB/s) reports
  the session moving average. To avoid flooding HA it publishes first at
  ~30 s into a session then at most once per minute, and reports `0` when
  idle. Enabled by default.

### Changed
- Unified `sync_status` to four states: `downloading`, `waiting`,
  `paused`, `error`. Replaces the previous `stopped` / `paused` /
  `downloading` / `idle` vocabulary on the Home Assistant
  `sensor.viofosync_sync_status` entity, and replaces the separate
  "Dashcam online / offline" badge in the web UI with a single status
  badge. If you have HA automations matching the previous strings,
  update them: `idle` and `stopped` map to `waiting` (or `paused`
  when sync is fully stopped). Connection state is still reflected
  via the existing `binary_sensor.viofosync_dashcam`.

## v2.1 — 2026-05-16

### Fixed

- DB now lives in the `/config` volume rather than the recordings  
mount for better performance when the recordings are on  
slower storage.
- The startup retention sweep runs in the background rather than  
blocking the UI when there's a large delete backlog.

### Changed

- Retention sweep logs progress: a header when the time-phase has  
work, then a line every 10 deletions. Previously silent until the  
end-of-sweep summary.

### Migration

- An existing DB at `${RECORDINGS}/.viofosync.db` is copied to  
`${CONFIG_DIR}/viofosync.db` on first boot under v2.1. The legacy  
file is renamed to `.viofosync.db.migrated` on the recordings  
volume as a recoverable fallback.

## v2.0 — 2026-05

Major rewrite. Web UI replaces the cron CLI.

### Added

- Web UI on port 8080 with archive browser, download manager, GPX  
journey map, and ffmpeg picture-in-picture exports.
- First-run setup wizard at `/setup`.
- Settings page (UI-driven config, hot-reloaded for runtime values,  
restart-required for `WEB_HOST`/`WEB_PORT`).
- JSON config at `/config/config.json` replaces `viofosync.env`.

### Changed

- Docker image is webapp-only; cron CLI is no longer the primary path.
- Required env vars reduced to `PUID` / `PGID` / `TZ`.

### Migration

- Existing `viofosync.env` files are migrated to `config.json` on first  
boot. The old file is preserved as a one-shot rollback path.

## v1.x

- Cron-driven CLI version. See git history.