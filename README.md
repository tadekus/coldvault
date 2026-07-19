# ❄ ColdVault

Self-hosted web app for securely archiving data to **Amazon S3 Deep Archive** and managing
restores. Runs locally in Docker (built for Debian), drives S3 exclusively through
`aws s3api`, and keeps a searchable local index + full audit log of everything it does.

## Features

- **Web UI** (default `http://localhost:9999`) — dashboard, searchable file index,
  upload sessions, restore management, live logs.
- **Canary-triggered USB ingest** — plug in an external drive containing a canary file
  (`coldvault.canary`) and its contents are automatically uploaded to your bucket.
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
  Switch buckets freely; the Index tab can filter per bucket or search across all,
  and restores always target the bucket the object was uploaded to.
- **Searchable index** — every uploaded object (key, size, SHA-256, ETag, timestamps,
  source path, session) is stored in SQLite. Search by any part of the key, select
  individual objects, and request **Standard** (~12 h) or **Bulk** (~48 h) restores.
  Deep Archive restores objects, not folders — the index is how you find the exact
  objects you need.
- **Restore tracking** — restore requests are logged and polled via `head-object` until
  S3 reports the object is available, including its expiry date.
- **Everything logged** — every `aws` command, upload, skip, failure, canary event and
  restore transition goes to the UI log, a rotating log file (`data/coldvault.log`) and
  the SQLite events table.

## Quick start

```bash
cp .env.example .env
# edit .env: AWS keys, region, bucket
docker compose up -d --build
# open http://localhost:9999
```

All configuration lives in `.env` — see [.env.example](.env.example) for every option.

### Verify it works

Open the dashboard and click **Test AWS connection** (runs `sts get-caller-identity` +
`head-bucket`). If your bucket already contains data, click **Import bucket → index** to
pull the existing object list into the local index (imported objects show as `remote`).

## The canary workflow

1. Create the canary file in the **root** of the external drive:

   ```bash
   touch /media/tadek/MYDRIVE/coldvault.canary
   ```

   Optionally give the drive a stable name (used as the S3 key prefix for its files):

   ```bash
   echo '{"name": "movie-drive-01", "prefix": "raw"}' > /media/tadek/MYDRIVE/coldvault.canary
   ```

   A plain-text file works too — the first line becomes the label. An empty file uses the
   volume folder name.

2. Plug the drive into the Debian box. Debian (udisks2) automounts it under
   `/media/<user>/<label>`, which is mounted read-only into the container.

3. ColdVault detects the canary and uploads everything as
   `<COLDVAULT_PREFIX>/<label>/<relative path>`, e.g.
   `raw/movie-drive-01/Movie_Project/Shooting_Day_01/Camera_A/Card_001/A001_C001.RAW`.

4. Watch progress in **Sessions**; every file lands in the **Index** with its checksum.

Files named like `.DS_Store`, `System Volume Information`, `.Trash-*` etc. are excluded
(configurable via `COLDVAULT_EXCLUDE`).

> If the drive mounts somewhere other than `/media`, add that path to both the
> docker-compose `volumes` list and `COLDVAULT_WATCH_DIRS`.

Set `COLDVAULT_AUTO_UPLOAD=false` if you want canary detection to only log the event so
you can start uploads manually from the dashboard.

## Restoring

Deep Archive objects must be restored before they can be downloaded:

1. **Index** tab → search (e.g. `Shooting_Day_02 .RAW`), tick the objects you need.
2. Pick **Bulk** (cheapest, ~48 h) or **Standard** (~12 h) and how many days the restored
   copy should stay available.
3. Track progress in **Restores** — ColdVault polls S3 hourly (configurable), or click
   **Check status now**.
4. Once `completed`, download with e.g.
   `aws s3api get-object --bucket <bucket> --key <key> <outfile>`.

## Notes

- `.env` holds live AWS credentials — keep it out of git (already in `.gitignore`) and
  readable only by you: `chmod 600 .env`.
- The IAM user only needs: `s3:PutObject`, `s3:GetObject`, `s3:ListBucket`,
  `s3:RestoreObject`, `s3:AbortMultipartUpload`, plus `sts:GetCallerIdentity` for the
  connection test and `s3:ListAllMyBuckets` for the bucket picker.
- `COLDVAULT_BUCKET` in `.env` is the initial bucket. You can also click
  **List buckets** on the dashboard and pick one there — that choice is persisted in
  `./data` and overrides the `.env` value until you pick another.
- The index/DB and logs persist in `./data` (bind-mounted to `/data`).
- The web UI has no authentication — bind it to localhost or your LAN only
  (e.g. `127.0.0.1:9999:9999` in docker-compose) and don't expose it to the internet.
- Multipart temp chunks are written to `data/tmp` — keep at least
  `COLDVAULT_PART_SIZE_MB × COLDVAULT_PART_WORKERS × COLDVAULT_UPLOAD_WORKERS` MB
  free there (~2 GB with defaults).
