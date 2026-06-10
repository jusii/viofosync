# Changelog

## v2.3 — 2026-06-10

### Added

#### Timeline Video Editor

A multi-camera timeline editor for the archive: scrub the front/rear channels on a shared playhead with per-clip filmstrips, set in/out trim points, and cut between cameras to export a single switched-angle video. Includes frame-accurate keyboard shortcuts. Join, picture-in-picture, and switched exports are now hardware-accelerated via Intel QuickSync where available, with VAAPI and software fallbacks.

#### Export Jobs

- Animated filmstrip preview on each job — hover to scrub through the finished video.
- Click a job's thumbnail to play the export in the viewer.
- Output **Length** and **Size** columns in the jobs table.
- Switched-camera exports now carry one continuous front-camera audio track, removing the audible jump at each camera switch.

### Changed

- Base image moved to Debian + jellyfin-ffmpeg to unlock QuickSync on Intel iGPUs; VAAPI and software remain as fallbacks, so a host without a working iGPU degrades transparently.
- Archive updates live on a clip-indexed push instead of per-client polling, and the per-day GPS route aggregation is cached.
- UI polish: background dither, clearer labels, and archive view state that persists across navigation.

### Fixed

A broad reliability and security hardening pass (full per-item detail in `CLAUDE.md`):

- **Worker lifecycle** — sync and export workers now shut down within a bounded timeout, and an in-flight or paused ffmpeg export is no longer left running after stop. Changing the dashcam address or toggling scheduled sync starts and stops the worker at runtime, without a restart.
- **Responsiveness** — NAS directory walks, SQLite transactions, and the quota disk-usage scan run off the event loop, so a slow or busy recordings volume no longer freezes the UI and live updates.
- **Data safety** — manual-import staging recovers completed clips after a crash instead of deleting them; downloads that fail their size check are rejected rather than archived truncated; corrupt or truncated MP4s no longer spin a worker at 100% CPU; and partial thumbnails, filmstrips, or exports can't be served from cache or left to count against the quota.
- **Disk full** — a full recordings volume raises a sticky "disk full" sync error and pauses the queue, instead of marking every clip failed.
- **MQTT** — reconnect backoff resets after a stable connection, retained discovery configs are cleaned up on the first node-id/prefix change, and timed-out probes are reaped.
- **Security** — clip filenames and geocoded place names are HTML-escaped before display; the live-events WebSocket rejects cross-origin handshakes; the MQTT broker password is no longer returned by the settings API; and retention will not delete a clip while an export is reading it.
- Queue counters update immediately after the Prioritise and Retry actions.

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
