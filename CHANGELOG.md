# Changelog

All notable changes to ColdVault. Versions follow `MAJOR.MINOR.PATCH`
(PATCH = fixes/tweaks, MINOR = features, MAJOR = breaking). The running version is
in [`app/version.py`](app/version.py) and shown in the web UI header.

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
