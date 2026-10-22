# Changelog

## Unreleased (2026-10-22)

### Fixed
- Deleting a watched sub-folder now removes its documents from the index. Windows reports a folder removal only as an extension-less delete event, which used to be dropped.
- Watched folders located under a directory named like a build/dependency folder (e.g. `D:/out/reports`, `D:/vendor/contracts`) were indexed and then purged on every start. Ignore rules now apply only below the watched root.
- Deleting or re-indexing a document with more than 32766 chunks failed with "too many SQL variables". IDs are now deleted in batches.
- "Re-index Now" no longer runs the DB purge on the API event loop or swaps the scanner state from another thread; the worker performs the reset itself.
- Nested or duplicate watched folders are collapsed, so files are no longer scanned and watched twice.
- Several DB helpers leaked a connection on error; blocking DB calls moved off the async event loop.
- Search UI: pressing Down with no results no longer sets the selection to -1.
- Settings UI now shows the folder list as stored by the backend (normalized, nested folders dropped), ignores stale save responses, and no longer sends duplicate requests under React StrictMode.

### Security
- The local API rejects requests with a non-loopback `Host` header (DNS rebinding) and requests from foreign `Origin`s (cross-site simple POSTs).
- The Vite dev-server origin (`http://localhost:5173`) is no longer trusted by the shipped backend; it is allowed only when running from source or when a debug Tauri build sets `COGNIFIND_DEV_ORIGIN=1`.

### Changed
- PDF text is extracted with PyMuPDF first (much faster on large PDFs), falling back to pypdf. Unchanged files that are already indexed are not re-extracted.

### Added
- Regression test suite: `python -m unittest discover -s tests -v`.
