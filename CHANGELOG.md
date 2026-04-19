# Integration manager Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

_Changes in the next release_

---

## v1.6.2 - 2026-04-18
### Fixed
- **SSL certificate verification** — GitHub API calls from the async `GitHubClient` now use a certifi-backed SSL context, resolving `CERTIFICATE_VERIFY_FAILED` errors when checking for updates on the remote.

---

## v1.6.1 - 2026-04-18
### Added
- **Self-update** — Integration Manager can now update itself directly from the web UI. Clicking it installs a temporary bootstrapper integration on the remote, which downloads the new IM release from GitHub, replaces the old installation, restores all settings and backups, then removes itself — no manual intervention required.
- **Firmware update check** — The diagnostics page now shows the current remote firmware version and highlights when a newer firmware release is available.

### Changed
- **Async web server** — Migrated from Flask to Quart (async-native). All route handlers are now `async`, enabling concurrent API calls without threading overhead.
- **Async API client** — `sync_api.py` rewritten to use `aiohttp` throughout, removing synchronous `requests` calls from route handlers.
- **Dependency updates** — `ucapi` bumped to `0.6.0`, `ucapi-framework` to `1.9.1`.

### Fixed
- **Docker backup support** — Integrations running in Docker containers can now be backed up correctly.
- **Entity reconfigure on update** — Fixed a bug where updating an integration would not restore your configured entities after an upgrade.

---

## v1.5.3 - 2026-03-12
### Added
- **Backup & restore for integrations** — One-click backup and restore of integration configurations, stored in `manager.json`. Backups survive Integration Manager updates and reinstalls.
- **Orphaned entity cleanup** — Detects and removes entity assignments left behind when an integration is deleted from the remote.

---

## v1.5.2 - 2026-02-23
### Added
- **Remote section in sidebar** — Quick-access links to the active remote's Web Configurator and Core REST API documentation
- **Integration log multi-select** — The service filter on the Integration Logs page now supports selecting multiple integrations simultaneously. Logs are merged and sorted newest-first. Download includes all selected services in a single file.
- **Styled log level picker** — The Log Level filter now uses the same custom dropdown style as the service selector, replacing the native browser select element.

### Fixed
- **Backup reliability** — Integration backups would intermittently fail because the manager polled for backup data before the integration had finished connecting to its device. The single fixed-delay GET is now replaced with a polling loop that waits up to 15 seconds for the integration to signal it is ready, eliminating the race condition.

---

## v1.0.2 - 2025-12-12
### Corrections
- More Pipeline fixes
- 
## v1.0.1 - 2025-12-12
### Corrections
- Pipeline fixes
  
## v1.0.0 - 2025-12-12
### Added
- Initial integration manager release based on ucapi-framework.
