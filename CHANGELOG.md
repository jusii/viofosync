# Changelog

## v2.2 — 2026-06-06

### Added

#### Home Assistant MQTT Support

Auto-discovered sensors and action buttons over MQTT, set up from a new Settings panel.

#### Manual Import

Add clips to the archive without Wi-Fi sync — by browser upload or a folder/USB drop path.

#### Alternative Camera Address

An optional second address for the same camera, used automatically when the primary is unreachable (e.g. reaching the dashcam over a mobile VPN).

#### Quota-Bound Retention

Measure retention and disk thresholds against a declared quota (`RECORDINGS_QUOTA_GB`), for recordings on a NAS share or ZFS dataset.

#### Sync Error Reporting

Sync now surfaces a sticky `error` state — missing config, unwritable path, camera auth failure, or disk full — in both the UI and Home Assistant.

#### Download Manager Improvements

Session speed and ETA while syncing, one-click retry of failed downloads, and live disk usage in Settings.

#### Export Improvements

Meaningful download filenames, direct download of the original front/rear clips, and a new rear-main picture-in-picture variant.

### Changed

- Sync status simplified to four states (`downloading` / `waiting` / `paused` / `error`); update any Home Assistant automations that matched the old `idle` / `stopped` strings.
- Export jobs panel redesigned.
- Downloads are now grouped by hour.
- UI polish: header alignment, unified status colours, and minor label tidy-ups.

### Fixed

- Archive retention caps now enforced on a periodic loop, not only after a download.
- Join exports no longer fail when clip paths are stored relative.
- Settings storage-usage card no longer renders near-invisible on the dark theme.

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
