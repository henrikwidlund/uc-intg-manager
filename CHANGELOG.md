# Integration manager Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

_Changes in the next release_

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
