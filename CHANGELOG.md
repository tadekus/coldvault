# Changelog

All notable changes to ColdVault. Versions follow `MAJOR.MINOR.PATCH`
(PATCH = fixes/tweaks, MINOR = features, MAJOR = breaking). The running version is
in [`app/version.py`](app/version.py) and shown in the web UI header.

## 1.11.0

- **Tree tab** — browse the archive as a lazily-expanded folder tree, with each folder
  showing its whole subtree's file count and size. Tick a **folder** to act on
  everything inside it (or tick individual files), then **Restore selected** or
  **Delete selected**. Restore and retention both accept prefixes, so a ticked folder
  expands server-side instead of shipping thousands of keys.
- **Delete permission is now visible in the app.** The Retention and Tree tabs probe
  whether the IAM user actually has `s3:DeleteObject` (a no-op delete of a random key
  under a reserved prefix — idempotent, removes nothing) and show either a green
  *“Append-only: this IAM user cannot delete”* banner or an amber *“CAN permanently
  delete”* warning. Delete buttons are disabled when the permission is absent.
- **Auto-eject after a successful upload** (`COLDVAULT_EJECT_AFTER_UPLOAD=true`). A
  canary upload that finishes with **zero failures** flags its drive as ready to
  unmount; a new host-side helper (`coldvault-eject` + systemd timer, installed by
  `deploy/usb-automount/install-udev.sh`) polls `/api/eject/pending`, unmounts, and
  reports back. A session with any failure is never flagged, and the log says why.
  The container never unmounts host filesystems itself.

## 1.10.0

- **Retention tab** — manage expiry and deletion of archived folders and files.
  - **Preview first:** enter a key prefix (a folder) and see exactly how many objects
    and bytes match, before anything happens.
  - **Cost awareness:** Deep Archive bills a 180-day minimum storage duration (Glacier
    90), so the preview flags objects still inside it and how many days you'd still be
    billed for. It also warns when **bucket versioning** is enabled, where a delete only
    adds a delete marker and the versions keep costing.
  - **Schedule expiry:** set or clear a planned deletion date. Nothing is ever deleted
    automatically — due items are listed for you to action deliberately.
  - **Delete permanently:** removes objects from S3 and the index. Guarded by a preview,
    a confirmation dialog and typing the bucket name; an empty match is refused.
  - **Audit trail:** every deleted object leaves a tombstone row (key, size, checksums,
    upload date, reason, mode, early-deletion days) in a new `deletions` table that
    outlives the index entry, plus full logging under a new `retention` category. A
    failed delete leaves the index row intact and writes no tombstone.

## 1.9.1

- **Install `tzdata` in the image.** Without it glibc can't resolve `TZ=Area/City`
  inside the container and silently falls back to UTC, so timestamps were written in
  UTC even though the app asks for local time (a CEST host showed 02:06 for a 04:06
  upload). Setting `TZ` in `.env` now works.
- Startup logs the container's local time, zone and UTC offset, and `/health` reports
  `timezone` — so a wrong clock is visible immediately instead of silently skewing
  every timestamp.

## 1.9.0

- **Sizes now use decimal (SI) units**, matching DIT/offload tools, drive capacities
  and Finder: 1 TB = 1000⁴ bytes. Previously ColdVault divided by 1024 but labelled the
  result "TB", so a 1.65 TB upload read as "1.5 TB" and disagreed with the DIT report —
  same bytes, wrong label. Applies to the UI, email reports and transfer speeds. Hover a
  size in the Index for the exact byte count.
- **All timestamps are local time.** S3-supplied values are now converted on ingest
  too — restore expiry (`…GMT`) and imported objects' `LastModified` (`…Z`) were
  previously stored and shown raw in UTC/GMT. Restore-expiry checks understand both the
  new local format and existing GMT rows, so nothing re-evaluates incorrectly.

## 1.8.1

- Fix verify-later reporting most files as "not archived" for Silverstack exports
  without a `Resources` column, whose `Name` is the bare clip name with no extension
  (`A_0002C003_…_h1DPN` for the archived `…_h1DPN.mxf`, `4-10T01` for `4-10T01.wav`).
  Such names now match archived files by filename stem. (Upload-time matching was
  already stem-aware and unaffected.)

## 1.8.0

- **ASC MHL support.** `.mhl` checksum manifests (ASC MHL v2 and older v1.x, as
  written per card by offload tools) are now read alongside CSVs — at upload time and
  in verify-later — so the whole offload, including sound (`.wav`), is covered, not
  just what the camera CSV lists. Also supports XXH3. Default
  `COLDVAULT_MANIFEST_EXTS` is now `.csv,.mhl`.
- **Verify against a manifest later** (Index tab). Drag & drop manifest file(s), or
  scan a server folder (e.g. a still-mounted drive) for every `.csv`/`.mhl` inside.
  Cross-checks each listed file against the archive — present / size mismatch / not
  archived — and attaches the manifest hash to the object, so a later restore +
  download re-verifies the bytes. Optional key-scope filter disambiguates repeated
  names (e.g. `4-10T01.wav` across shoot days).
- **Deep verify** (opt-in, background): if the source files are still on a mounted
  drive, re-hash them; when the local SHA-256 equals the archived object's, the
  manifest comparison is a true byte-level verification of the archive — no restore
  needed. Progress shown live; results become `csv✓` / `csv✗`.
- **Diagnostics.** Startup logs whether the manifest check is on and whether `xxhash`
  is installed (ERROR with the rebuild command if not); `/health` gains a
  `manifest_check` block; every upload logs a one-line manifest summary (or that no
  manifest was found). New `csv·` badge = listed in a manifest, bytes not yet re-hashed.

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
