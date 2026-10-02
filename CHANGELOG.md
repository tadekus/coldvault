# Changelog

All notable changes to ColdVault. Versions follow `MAJOR.MINOR.PATCH`
(PATCH = fixes/tweaks, MINOR = features, MAJOR = breaking). The running version is
in [`app/version.py`](app/version.py) and shown in the web UI header.

## 1.7.0

- **`/health` monitoring endpoint.** Reports upload / restore / download status plus
  integrity signals (failed uploads, manifest mismatches, audit issues) as JSON. Reads
  only the local DB + in-memory state — no AWS calls — so it's cheap to poll. `status`
  is `ok` / `busy` / `attention`; `?strict=1` returns HTTP 503 on `attention` for uptime
  monitors; `?check_aws=1` adds a live AWS-connectivity check.

## 1.6.0

- **Byte-level manifest re-verify on download.** When a restored object is downloaded,
  its offload-manifest hash (XXH64/…) is recomputed in the same read pass as the
  verification SHA-256 and compared to the value recorded at upload — a true check that
  the restored bytes match what the DIT recorded at offload. Result shows as a `csv✓` /
  `csv✗` badge in the Downloaded-files table and is logged (mismatch → error). Negligible
  cost: no extra read pass, and XXH64 is far faster than the SHA-256 already computed.
  Adds a `manifest_state` column to the downloads table.

## 1.5.0

- **Audit re-surfaces the offload-manifest check.** The bucket audit now reports how
  many objects are manifest-verified vs mismatched, flags any mismatch as an audit
  finding (so it shows up in every audit and email report, not just at upload), and logs
  mismatches as errors. Deep Archive can't be re-hashed without a restore, so this
  re-checks the manifest result recorded at upload rather than the object bytes.

## 1.4.0

- **Offload manifest cross-check.** If a source folder contains a checksum manifest CSV
  from a DIT/offload tool (Silverstack, YoYotta, Pomfort, …), ColdVault matches each
  uploaded file to it by name and verifies the manifest's hash (XXH64/MD5/SHA1/SHA256),
  computed in the same read pass as the upload SHA-256 — an independent check that the
  file on the drive matches what was recorded at offload. Results show as `csv✓` /
  `csv✗` badges in the Index, are filterable (**manifest: mismatch/ok/not in manifest**),
  and mismatches are logged as errors (category `manifest`). Toggle with
  `COLDVAULT_MANIFEST_CHECK`; manifest extensions via `COLDVAULT_MANIFEST_EXTS`
  (default `.csv`). Adds the `xxhash` dependency.

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
