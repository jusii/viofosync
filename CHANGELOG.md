# Changelog

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
