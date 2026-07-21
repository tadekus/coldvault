# ❄ ColdVault

Self-hosted web app for securely archiving data to **Amazon S3 Deep Archive** and managing
restores. Runs locally in Docker — tested on **Debian/Linux and macOS** — drives S3
exclusively through `aws s3api`, and keeps a searchable local index + full audit log of
everything it does.

## Features

- **Web UI** (default `http://localhost:9999`) — dashboard, searchable file index,
  upload sessions with live progress and speed, restore management, verified
  downloads of restored files, live logs.
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
- **Verified downloads** — restored objects are fetched back to a local folder with
  their key structure preserved, using parallel ranged GETs for large files, and each
  file's SHA-256 is checked against the index before it counts as `verified`.
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

## Running, restarting and updating

Everything below is run from the ColdVault folder (e.g. `~/coldvault`). On macOS add
`-f docker-compose.mac.yml` to every command.

```bash
# restart (after changing .env, or if it got stuck)
docker compose restart

# stop / start
docker compose down
docker compose up -d

# update to the latest version from GitHub, then rebuild and restart
git pull
docker compose up -d --build

# follow the logs
docker compose logs -f
# ...or the app's own rotating log
tail -f data/coldvault.log

# status
docker compose ps
```

Notes:

- `docker compose restart` reloads `.env`, but **volume changes in
  `docker-compose.yml` need `docker compose up -d`** (recreates the container);
  add `--force-recreate` if a change doesn't seem to take.
- Rebuild with `--build` whenever the app code or `requirements.txt` changed —
  i.e. after every `git pull`.
- After an update, **hard-refresh the browser** (Ctrl+Shift+R, Cmd+Shift+R on macOS)
  so the new UI assets load instead of the cached ones.
- New releases sometimes add settings to `.env.example`. Compare it with your `.env`
  after pulling: `diff <(sort .env.example) <(sort .env) | head -30` — anything
  missing falls back to a sane default, so this is optional but worth a look.
- Your `.env`, index database (`data/`) and downloaded files (`downloads/`) are
  never touched by updates — they are git-ignored.
- Uploads or downloads interrupted by a restart are marked failed and can simply be
  re-run: verified files are skipped, so it resumes where it left off.

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
4. Once `completed`, fetch the files in the **Downloads** tab (below).

## Downloading restored files

The **Downloads** tab lists every object whose restore has completed — i.e. everything
downloadable right now — and pulls it back to a local folder:

1. Set the destination (defaults to `COLDVAULT_DOWNLOAD_DIR`, `/downloads` in the
   container) and optionally a subfolder per job, e.g. `SD13_conform`.
2. Tick the objects you want, or **Select all restored**.
3. **Download selected** — files are written under the destination preserving their
   full S3 key as folders, so
   `Movie_Project/Shooting_Day_01/Camera_A/Card_001/A001_C001.RAW` comes back as that
   same directory tree.

Every download is **checksum-verified**: after the transfer, the file's SHA-256 is
compared against the value recorded in the index when it was uploaded, and only then
marked `verified` (objects imported with **Import bucket → index** have no stored
checksum, so they are marked `downloaded` instead). Size mismatches, unrestored
objects and restores still in progress fail with an explicit message rather than
leaving a corrupt file. Files already present locally with a matching size and
checksum are skipped, so an interrupted job resumes cheaply.

Large objects are fetched with **parallel ranged GETs**
(`COLDVAULT_DOWNLOAD_PART_WORKERS`, default 4) written directly into the target file,
mirroring the parallel multipart upload path. Progress, per-part speed and per-file
speed appear in the session table and the log.

> The download directory must be mounted **read-write** in docker-compose (unlike the
> source volumes, which are read-only). The default maps `./downloads`; point it
> anywhere you like, e.g. `- /tank/restores:/downloads`. Downloads are refused
> outside this directory.

### Restore from an edit (XML / AAF)

Instead of hand-picking objects, upload an editorial exchange file in the **Index**
tab (**Match edit list**) and ColdVault gathers every media file the edit references
and prepares the batch restore:

- **Final Cut Pro 7 / Premiere Pro XML** (`.xml`, xmeml — `<pathurl>` references)
- **FCPXML** (`.fcpxml` — Final Cut Pro X / DaVinci Resolve, `<media-rep src=…>`
  references)
- **Avid AAF** (`.aaf` — parsed with pyaaf2, reading each SourceMob's
  NetworkLocator; falls back to a raw string scan for unusual AAFs)

Because the paths inside an edit point at the editor's local volumes, matching is
done **by filename** (case-insensitive) against the index — which works because
camera-original names (`A001_C001.RAW`, `A035C014_260625I0.mxf`, …) are unique per
production. All matched objects are added to the selection (scoped to the bucket
chosen in the bucket filter, or all buckets), anything the index doesn't contain is
listed as *not found*, and one click on **Request restore** sends the batch
(Standard or Bulk). The parse result is logged like everything else.

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
