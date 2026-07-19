# ❄ ColdVault

Self-hosted web app for securely archiving data to **Amazon S3 Deep Archive** and managing
restores. Runs locally in Docker — tested on **Debian/Linux and macOS** — drives S3
exclusively through `aws s3api`, and keeps a searchable local index + full audit log of
everything it does.

## Features

- **Web UI** (default `http://localhost:9999`) — dashboard, searchable file index,
  upload sessions with live progress and speed, restore management, live logs.
- **Canary-triggered ingest** — plug in an external drive containing a canary file
  (`coldvault.canary`) and its contents are automatically uploaded to your bucket.
  Works with Debian's `/media` automounts and macOS `/Volumes` alike.
- **Verified uploads** — every object is uploaded with a SHA-256 checksum
  (`--checksum-sha256`), so S3 rejects corrupted transfers; the checksum returned by S3 is
  compared against the locally computed one before a file is marked `verified`.
  Large files use `s3api` multipart upload with **parallel part uploads**
  (`COLDVAULT_PART_WORKERS`, default 4) — the same technique the CRT transfer client
  uses — with per-part SHA-256 checksums and composite checksum verification.
- **Incremental + deduplicated** — re-plugging a drive re-scans it but skips files
  already verified (matched by key + size + mtime, then by SHA-256 if the timestamp
  changed), so interrupted uploads simply resume. With `COLDVAULT_DEDUPE=true`
  (default) a file whose exact content (SHA-256 + size) is already archived in the
  bucket under *any* key is skipped too, and the log tells you which object it
  duplicates — applies to both canary and manual uploads.
- **Multi-bucket aware** — the index records which bucket every object lives in.
  Switch buckets freely (pick one from the UI), the Index tab can filter per bucket or
  search across all, and restores always target the bucket the object was uploaded to.
- **Searchable index** — every uploaded object (key, size, SHA-256, ETag, timestamps,
  upload speed, source path, session) is stored in SQLite. Search by any part of the
  key, select individual objects, and request **Standard** (~12 h) or **Bulk** (~48 h)
  restores. Deep Archive restores objects, not folders — the index is how you find the
  exact objects you need.
- **Restore tracking** — restore requests are logged and polled via `head-object` until
  S3 reports the object is available, including its expiry date.
- **Everything logged** — every `aws` command, upload, skip, failure, canary event and
  restore transition goes to the UI log, a rotating log file (`data/coldvault.log`) and
  the SQLite events table.

## Installation

Requirements: Docker (with Compose) and an AWS IAM user — see [IAM policy](#notes) below.

```bash
git clone https://github.com/tadekus/coldvault.git
cd coldvault
cp .env.example .env
# edit .env: AWS keys, region, bucket
```

All configuration lives in `.env` — see [.env.example](.env.example) for every option.

### Debian / Linux

```bash
docker compose up -d --build
# open http://localhost:9999
```

[docker-compose.yml](docker-compose.yml) mounts `/media` (where udisks2 automounts
USB/external drives) into the container. If your drives mount elsewhere (e.g. `/mnt`),
add that path to the compose `volumes` and to `COLDVAULT_WATCH_DIRS`.

### macOS

```bash
docker compose -f docker-compose.mac.yml up -d --build
# open http://localhost:9999
```

[docker-compose.mac.yml](docker-compose.mac.yml) maps `/Volumes` (where macOS mounts
USB/external drives) to `/media` inside the container, so the canary workflow behaves
identically to Linux. Use `-f docker-compose.mac.yml` on **every** compose command
(including `down`). Each machine keeps its own independent `./data` index database.

### Verify it works

Open the dashboard and click **Test AWS connection** (runs `sts get-caller-identity` +
`head-bucket`). Click **List buckets** to pick your target bucket from the account —
the choice is persisted and overrides `.env`. If your bucket already contains data,
click **Import bucket → index** to pull the existing object list into the local index
(imported objects show as `remote`).

## The canary workflow

1. Create the canary file in the **root** of the external drive:

   ```bash
   # Debian/Linux (drive automounted by udisks2):
   touch /media/$USER/MYDRIVE/coldvault.canary

   # macOS:
   touch /Volumes/MYDRIVE/coldvault.canary
   ```

   Optionally give the drive a stable name (used as the S3 key prefix for its files):

   ```bash
   echo '{"name": "movie-drive-01", "prefix": "raw"}' > /path/to/drive/coldvault.canary
   ```

   A plain-text file works too — the first line becomes the label. An empty file uses
   the volume folder name.

2. Plug the drive in. The watcher polls the watch roots (Debian: `/media`,
   macOS: `/Volumes` via the container's `/media`) every `COLDVAULT_WATCH_INTERVAL`
   seconds.

3. ColdVault detects the canary and uploads everything as
   `<COLDVAULT_PREFIX>/<label>/<relative path>`, e.g.
   `raw/movie-drive-01/Movie_Project/Shooting_Day_01/Camera_A/Card_001/A001_C001.RAW`.

4. Watch progress in **Sessions** (files, bytes, percentage, per-part speed for large
   files); every file lands in the **Index** with its checksum. Re-plugging the drive
   later uploads only new or changed files.

OS junk (`.DS_Store`, `System Volume Information`, `.Trash-*`, resource forks etc.) is
excluded — configurable via `COLDVAULT_EXCLUDE`.

Set `COLDVAULT_AUTO_UPLOAD=false` if you want canary detection to only log the event so
you can start uploads manually from the dashboard.

> ⚠️ Auto-upload sends **everything on the drive** to Deep Archive, which has a
> 180-day minimum storage charge per object (deleting earlier still bills the full
> 180 days). When testing, use a drive containing only a small folder — or disable
> auto-upload and use a manual upload instead.

## Manual uploads

Any folder can be uploaded from the dashboard (path field or **Browse**), as long as
it is (a) bind-mounted into the container and (b) listed in `COLDVAULT_BROWSE_ROOTS`.
Example for a NAS/ZFS path on Linux — in `docker-compose.yml`:

```yaml
volumes:
  - /tank/nextcloud/data:/tank/nextcloud/data:ro
```

and in `.env`:

```
COLDVAULT_BROWSE_ROOTS=/tank/nextcloud/data
```

(Keep the same path on both sides so paths typed in the UI match. On macOS, see the
commented `testdata` mount in `docker-compose.mac.yml`.) The label field controls the
S3 key prefix; empty defaults to the folder name.

## Restoring

Deep Archive objects must be restored before they can be downloaded:

1. **Index** tab → search (e.g. `Shooting_Day_02 .RAW`), tick the objects you need.
2. Pick **Bulk** (cheapest, ~48 h) or **Standard** (~12 h) and how many days the restored
   copy should stay available.
3. Track progress in **Restores** — ColdVault polls S3 hourly (configurable), or click
   **Check status now**.
4. Once `completed`, download with e.g.
   `aws s3api get-object --bucket <bucket> --key <key> <outfile>`.

## Performance tuning

A single S3 PUT stream tops out around 20–40 MB/s. ColdVault parallelizes at two
levels: `COLDVAULT_UPLOAD_WORKERS` files at once, and `COLDVAULT_PART_WORKERS`
multipart parts per large file. Suggested settings for very large files (20–40 GB)
on a ~1 Gbps uplink:

```
COLDVAULT_MULTIPART_THRESHOLD_MB=512
COLDVAULT_PART_SIZE_MB=512
COLDVAULT_PART_WORKERS=4
COLDVAULT_UPLOAD_WORKERS=2
```

Part staging uses temp space in `data/tmp`:
`PART_SIZE_MB × PART_WORKERS × UPLOAD_WORKERS` (put `./data` on an SSD if possible).
The per-part speed in the logs shows immediately whether extra workers add throughput
or just divide it.

## Notes

- `.env` holds live AWS credentials — keep it out of git (already in `.gitignore`) and
  readable only by you: `chmod 600 .env`.
- The IAM user needs: `s3:ListAllMyBuckets` (bucket picker), `s3:ListBucket` on the
  bucket, and `s3:PutObject`, `s3:GetObject`, `s3:RestoreObject`,
  `s3:AbortMultipartUpload` on the bucket's objects, plus `sts:GetCallerIdentity`
  for the connection test.
- `COLDVAULT_BUCKET` in `.env` is the initial bucket. You can also click
  **List buckets** on the dashboard and pick one there — that choice is persisted in
  `./data` and overrides the `.env` value until you pick another.
- The index/DB and logs persist in `./data` (bind-mounted to `/data`). The database
  (`data/coldvault.db`) is your file inventory — worth backing up occasionally.
- The web UI has no authentication — bind it to localhost or your LAN only
  (e.g. `127.0.0.1:9999:9999` in docker-compose) and don't expose it to the internet.
