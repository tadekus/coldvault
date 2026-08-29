# Changelog

All notable changes to ColdVault. Versions follow `MAJOR.MINOR.PATCH`
(PATCH = fixes/tweaks, MINOR = features, MAJOR = breaking). The running version is
in [`app/version.py`](app/version.py) and shown in the web UI header.

## 1.3.0

- **Clear local index** (dashboard → Connection). Wipe index metadata for one bucket
  (e.g. after deleting/migrating a bucket) or the whole database, guarded by a typed
  confirmation (the bucket name, or `ALL`). Only the local index is affected — S3 data
  is never touched, and the active-bucket setting is preserved.

## 1.2.0

- **Local download integrity check.** The Downloads tab now tracks whether each
  restored object's downloaded file still exists on disk. **Check local files** re-scans
  and flags any that were deleted, so a removed file no longer appears as downloaded.
- Restored list shows each object's **local state** (on disk / deleted / not downloaded)
  and marks restores whose **window has expired** (their checkbox is disabled — re-request
  in the Index tab). New **Select not-downloaded** helper skips redundant re-downloads.
- Disk-space pre-flight now ignores files already present at the destination (they'd be
  skipped anyway), so the fit check reflects what will actually be fetched; a selection
  that's entirely on disk proceeds even on a full disk.

## 1.1.1

- Fix Resend calls failing with Cloudflare **403 error 1010**: send a real
  `User-Agent` (and `Accept`) header instead of the blocked `Python-urllib` default.
- Move **Email notifications** off the Dashboard into its own top-right tab.

## 1.1.0

- **Email notifications via Resend.** After each canary/watcher upload, ColdVault runs
  a bucket audit and emails a report (what was archived, current bucket size, audit
  status) when `COLDVAULT_NOTIFY=true`. Configure `RESEND_API_KEY`,
  `COLDVAULT_EMAIL_FROM`, `COLDVAULT_EMAIL_TO` in `.env`.
- Dashboard **Email notifications** box: **Email audit report now** (on-demand audit +
  report) and **Send test email**.
- Audit logic extracted into a reusable module shared by the endpoint and the hook;
  audit report now includes total bucket size.

## 1.0.0

First versioned build. Established feature set:

- Dockerized Flask app driving `aws s3api`; Debian and macOS compose variants.
- Canary-triggered USB ingest with a read-only auto-mount package for headless
  Debian (`deploy/usb-automount/`).
- Checksum-verified uploads (SHA-256), parallel multipart for large files.
- Multi-bucket searchable index (SQLite), newest-first by default; per-file upload
  speed; content-based dedupe.
- Standard/Bulk restore management with polling; restore-from-edit-list matching for
  FCP7 XML / FCPXML / AAF.
- Verified downloads of restored objects with parallel ranged GETs and a disk-space
  fit-or-cancel pre-flight.
- On-demand bucket integrity audit (missing / size-mismatch / class-drift).
- Local-time timestamps; full audit log to UI, file and DB.
- Robust canary sessions across restarts (no duplicate/zombie sessions).
- Default excludes now cover `.nextcloud-sync-canary` / `.nextcloudignore`.
