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
  (`coldvault.canary`) and its contents are automatically uploaded to your bucket —
  or to the bucket the canary names, so each drive can go to its own production's
  bucket. Works with Debian's `/media` automounts and macOS `/Volumes` alike.
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
  duplicates — applies to both canary and manual uploads. A large file is read
  ahead of its upload only when a duplicate is possible at all (another archived
  object has exactly its size) or the same name was archived with the same size;
  otherwise its checksums — and its offload-manifest hash — are computed during the
  upload's own read, so it is read once, not twice.
- **Multi-bucket aware** — the index records which bucket every object lives in.
  Switch buckets freely (pick one from the UI), the Index tab can filter per bucket or
  search across all, and restores always target the bucket the object was uploaded to.
- **Searchable index** — every uploaded object (key, size, SHA-256, ETag, timestamps,
  upload speed, source path, session) is stored in SQLite. Search by any part of the
  key, select individual objects, and request **Standard** (~12 h) or **Bulk** (~48 h)
  restores. Deep Archive restores objects, not folders — the index is how you find the
  exact objects you need. The Index defaults to **newest-first** so a fresh upload
  shows at the top; switch to **Name A–Z** with the sort dropdown.
- **Tree view** — browse the archive as a folder tree (expanded lazily, each folder
  showing its subtree's file count and size) and act on a whole folder by ticking it:
  restore everything inside it, or delete it.
- **Retention with a visible safety net** — schedule expiry dates, preview exactly what
  a deletion covers (including the Deep Archive 180-day minimum you'd still be billed
  for), and delete with an audit trail. The app also *tells you* whether its IAM user
  can delete at all, so an append-only setup is provable rather than assumed.
- **Auto-eject** — optionally mark a canary drive as safe to unplug once its upload
  finishes with zero failures; the host-side helper does the unmount.
- **Offload manifest cross-check** — if a DIT/offload checksum manifest (Silverstack,
  YoYotta, …) sits in the source folder, ColdVault verifies each file against its
  recorded XXH64/MD5/SHA-256 hash during upload — catching corruption between offload
  and archive.
- **Integrity audit** — reconcile the index against the actual bucket contents on
  demand: flags objects the index thinks are archived but are missing from S3, size
  mismatches, and storage-class drift, and imports anything new. Paginated, so it
  scales past 1000 objects.
- **Email reports (Resend)** — after each canary upload, optionally run an audit and
  email a report (what was archived, bucket size, audit status); or send one on demand.
- **Restore tracking** — restore requests are logged and polled via `head-object` until
  S3 reports the object is available, including its expiry date.
- **Verified downloads** — restored objects are fetched back to a local folder with
  their key structure preserved, using parallel ranged GETs for large files, and each
  file's SHA-256 is checked against the index before it counts as `verified`.
- **Everything logged** — every `aws` command, upload, skip, failure, canary event and
  restore transition goes to the UI log, a rotating log file (`data/coldvault.log`) and
  the SQLite events table.
- **Local backup of its own state** — a daily, consistent snapshot of the database,
  logs and config in `./backups/latest`, ready for rsync, borg or restic.
- **A second machine (agent)** — e.g. a Raspberry Pi next to the master uploads its own
  drives and receives downloads, at the same time as the master, with one shared index
  and one web UI.

## Installation

Requirements: Docker (with Compose) and an AWS IAM user — see
[AWS users and permissions](#aws-users-and-permissions) for how to set it up properly.

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

## AWS users and permissions

ColdVault is built so that **nothing running day to day can delete your archive**.
That takes two separate AWS identities (three with an agent):

| Identity | Type | Used by | Can | Can never |
|---|---|---|---|---|
| **Everyday user** (e.g. `coldvault-master`) | IAM user, keys in the master's `.env` | the master, all the time | upload, restore, download, list, audit, and *ask* to unlock the delete role | delete — an explicit **Deny**, so a broader policy attached later can't change that |
| **Delete role** (`ColdVaultDelete`) | IAM role, no keys of its own | the master, only while a deletion you confirmed is running | delete objects in your buckets | anything else |
| **Agent user** (e.g. `coldvault-agent`) | IAM user, keys in the agent's `.env` | a [second machine](#second-machine-agent) | upload, download | delete, restore, list |

**Why a role, not a second user with delete rights?** A second user's keys would sit in
`.env` all the time, so a stolen backup or a compromised machine could delete
everything. A role has no keys: the everyday user can only *ask* for temporary role
credentials, and the role's trust policy grants that **only with a fresh code from your
authenticator app (MFA)**. When you delete, ColdVault asks for the code as the last
step, borrows the role for 15 minutes, deletes, and throws the credentials away. A wrong
code means nothing is deleted, and every deletion shows up in AWS CloudTrail as an
MFA-verified role session.

You don't have to set up the role: without it ColdVault is simply **append-only** (the
Retention tab can schedule expiry, but its delete buttons stay off). Add the role when
you actually want to delete.

### What you need

- An **administrator** login for your AWS account — the web console, or an admin
  profile for the AWS CLI **on your own computer**. Never give ColdVault admin keys, and
  never use the account's **root** user's keys for ColdVault.
- Your 12-digit **account number** and the names of your **buckets**.
- An authenticator app on your phone (Google Authenticator, 1Password, Authy, …) for MFA.

### 1. Check what ColdVault uses today

If ColdVault is already running, ask AWS which identity its keys belong to (read-only;
run on the master, in the ColdVault folder):

```bash
cd ~/coldvault && ACCOUNT=$(docker compose exec -T coldvault aws sts get-caller-identity --query Account --output text) && ARN=$(docker compose exec -T coldvault aws sts get-caller-identity --query Arn --output text) && echo "account: $ACCOUNT  identity: $ARN"
```

- `…:user/<name>` — an IAM user. **If that user is used by ColdVault only**, keep it as
  the everyday user. If it's also your own console login or used by other scripts,
  **create a separate everyday user** instead: the policy below blocks deleting in your
  ColdVault buckets for whichever user carries it — in the console too — and you'd
  remove its other permissions.
- `…:root` — the account's root keys. Replace them with an everyday user right away,
  then delete those root keys in the console (*Security credentials* of the root user).

### 2. Write the policies

The script fills in your account number and buckets and writes four policy files. It
makes **no AWS calls**; the files go to `deploy/iam/out/`, which git ignores.

```bash
cd ~/coldvault/deploy/iam
./make-policies.sh 123456789012 my-archive-bucket second-bucket      # your account + all your buckets
```

Use **your real values**, without angle brackets — `<account-id>` typed literally makes
bash answer *"No such file or directory"*. Straight after step 1 you can let the shell
fill them in, and keep the existing user's name:

```bash
cd ~/coldvault/deploy/iam && MASTER_USER="${ARN##*/}" ./make-policies.sh "$ACCOUNT" my-archive-bucket second-bucket
```

Without `MASTER_USER` the everyday user is called `coldvault-master` (`AGENT_USER` and
`DELETE_ROLE` change the other names). The four files:

| File | Goes on | Says |
|---|---|---|
| `master-user.json` | the everyday user | allow list/upload/restore/download, allow `sts:AssumeRole` on the delete role, **deny** `s3:DeleteObject` |
| `delete-role-trust.json` | the delete role's *trust policy* | only the everyday user may assume it, and only with MFA (code less than 5 minutes old) |
| `delete-role.json` | the delete role's permissions | allow `s3:DeleteObject` on your buckets' objects — nothing else |
| `agent-user.json` | an agent user | allow upload/download, **deny** delete |

Read them — they're short. For example, the trust policy:

```json
{"Effect": "Allow",
 "Principal": {"AWS": "arn:aws:iam::123456789012:user/coldvault-master"},
 "Action": "sts:AssumeRole",
 "Condition": {"Bool": {"aws:MultiFactorAuthPresent": "true"},
               "NumericLessThan": {"aws:MultiFactorAuthAge": "300"}}}
```

### 3. Create them — in the AWS console

Signed in as an administrator, in **IAM**:

1. **Everyday user** *(keeping an existing user? Do only step 2 here — its keys are already in `.env`)*
   1. *Users → Create user*, name `coldvault-master`, **no** console access → *Next* →
      leave permissions empty → *Create user*.
   2. Open the user → *Permissions → Add permissions → Create inline policy → JSON* →
      paste `master-user.json` → *Next*, name it `coldvault` → *Create policy*. If the
      user has other policies attached (e.g. `AmazonS3FullAccess`), remove them — the
      inline policy is everything ColdVault needs.
   3. *Security credentials → Create access key → Application running outside AWS* →
      copy both keys into the master's `.env` (`AWS_ACCESS_KEY_ID`,
      `AWS_SECRET_ACCESS_KEY`). The secret is shown only once.
2. **MFA for the everyday user** — open the user → *Security credentials → Assign MFA
   device* → name it (e.g. `coldvault-master`) → *Authenticator app* → scan the QR code
   and enter two consecutive codes. Copy the device's **Identifier**, e.g.
   `arn:aws:iam::123456789012:mfa/coldvault-master`.
3. **Delete role** — *Roles → Create role → Custom trust policy* → paste
   `delete-role-trust.json` → *Next* → add no permissions → *Next* → name
   `ColdVaultDelete` → *Create role*. Open it → *Permissions → Add permissions → Create
   inline policy → JSON* → paste `delete-role.json` → name `delete` → *Create policy*.
   Copy the role's **ARN** from its summary.
4. **Agent user** (only with a [second machine](#second-machine-agent)) — as in 1, with
   `agent-user.json`; its keys go into the agent's `.env`. One user per agent lets you
   revoke a single machine.

### 3. …or with the AWS CLI

With an administrator profile (here `admin`) on your own computer, in `deploy/iam/`:

```bash
P="--profile admin"
# everyday user (skip create-user and create-access-key if you keep an existing one)
aws iam create-user --user-name coldvault-master $P
aws iam put-user-policy --user-name coldvault-master --policy-name coldvault --policy-document file://out/master-user.json $P
aws iam create-access-key --user-name coldvault-master $P      # -> the master's .env
# MFA: assign it in the console ("MFA for the everyday user" above) — it needs your phone anyway
# delete role (after the user exists: AWS checks the trust policy's user)
aws iam create-role --role-name ColdVaultDelete --assume-role-policy-document file://out/delete-role-trust.json $P
aws iam put-role-policy --role-name ColdVaultDelete --policy-name delete --policy-document file://out/delete-role.json $P
# agent user
aws iam create-user --user-name coldvault-agent $P
aws iam put-user-policy --user-name coldvault-agent --policy-name coldvault --policy-document file://out/agent-user.json $P
aws iam create-access-key --user-name coldvault-agent $P       # -> the agent's .env
```

### 4. Tell ColdVault, and test

In the master's `.env` (role ARN and MFA identifier from step 3):

```
COLDVAULT_DELETE_ROLE_ARN=arn:aws:iam::123456789012:role/ColdVaultDelete
COLDVAULT_DELETE_MFA_SERIAL=arn:aws:iam::123456789012:mfa/coldvault-master
```

Apply with `docker compose up -d` (a plain `restart` doesn't re-read `.env`). Then in
**Retention**:

1. **Re-check** — every bucket should show 🔒 *append-only* for the everyday user, plus
   🔐 *deleting goes through the delete role*.
2. **Test delete role** — enter a code. It unlocks the role and runs a harmless probe
   (a delete of a random key that never existed): *"the role can delete from this
   bucket — nothing was removed"*.

From then on every delete — Retention or Tree — ends with *"Enter the 6-digit code from
your authenticator app"*.

### Adding a bucket later

Run `make-policies.sh` again with the **full** list of buckets and apply the new files:
in the console, edit the inline policies (`coldvault` on the users, `delete` on the
role); with the CLI, repeat the `put-user-policy` / `put-role-policy` commands. A bucket
missing from `delete-role.json` shows in **Test delete role** as *"has no
s3:DeleteObject on this bucket"*.

### Buckets in other regions

Buckets don't have to be in `AWS_DEFAULT_REGION`. Deep Archive costs differ a lot by
region — at the time of writing $1.80 per TB-month in Frankfurt (`eu-central-1`) but
$0.99 in Stockholm (`eu-north-1`) and Ireland (`eu-west-1`) — so a new bucket can go
somewhere cheaper. Nothing to configure: the first time ColdVault uses a bucket, the
master asks S3 for its region once (`head-bucket`), keeps the answer in its database,
and from then on sends every request for that bucket straight there; agents receive
the same list with their settings. The bucket pickers show each bucket's region
(`s3://my-archive-bucket · eu-north-1`). A bucket whose region isn't known yet still
works — S3 redirects the request and the CLI follows.

Existing Deep Archive objects can't be moved to another region without restoring them
(and paying the early-deletion charge for anything younger than 180 days), so a cheaper
region is for **new** uploads: create the bucket there and add it as in *Adding a bucket
later*.

### If something doesn't work

| Symptom | Cause |
|---|---|
| `-bash: account-id: No such file or directory` | an `<…>` placeholder typed literally — use the real value |
| *wrong or already-used code* | each code works once and only for ~30 s; wait for the next one. Check the phone's clock is set automatically |
| *not authorized to perform: sts:AssumeRole* | the everyday user lacks `UnlockDeleteRole` (re-apply `master-user.json`), the trust policy names a different user, or `COLDVAULT_DELETE_MFA_SERIAL` isn't that user's device |
| *has no s3:DeleteObject on this bucket* | the bucket isn't in `delete-role.json` — see *Adding a bucket later* |
| Retention says the everyday user **CAN** delete | it still has another policy granting delete — remove it; with `master-user.json` applied the Deny wins |
| *Test AWS connection* fails after the switch | new keys not in `.env`, or `docker compose up -d` not run |

## Running, restarting and updating

Everything below is run from the ColdVault folder (e.g. `~/coldvault`). On macOS add
`-f docker-compose.mac.yml` to every command.

```bash
# apply .env changes (recreates the container with the new settings)
docker compose up -d

# restart as-is (if it got stuck)
docker compose restart

# stop / start
docker compose down
docker compose up -d

# update to the latest version from GitHub, then rebuild and restart
git pull
./env-check.sh          # anything new in .env.example?
docker compose up -d --build

# follow the logs
docker compose logs -f
# ...or the app's own rotating log
tail -f data/coldvault.log

# status
docker compose ps
```

Notes:

- **`.env` and volume changes need `docker compose up -d`**, which recreates the
  container. `docker compose restart` restarts the existing container with the settings
  it was created with, so an edited `.env` is *not* picked up. Add `--force-recreate`
  if a change doesn't seem to take.
- Rebuild with `--build` whenever the app code or `requirements.txt` changed —
  i.e. after every `git pull`.
- **Machine-specific source-folder mounts go in `docker-compose.override.yml`**, not
  in `docker-compose.yml`. That file is git-ignored and auto-merged by
  `docker compose up`, so `git pull` never conflicts with your local mounts and they
  survive every update. Copy `docker-compose.override.example.yml` to get started
  (see [Manual uploads](#manual-uploads)). New source folders take effect with
  `docker compose up -d`.
- After an update, **hard-refresh the browser** (Ctrl+Shift+R, Cmd+Shift+R on macOS)
  so the new UI assets load instead of the cached ones.
- New releases sometimes add settings to `.env.example`. After pulling, run
  **`./env-check.sh`**. It lists the settings your `.env` doesn't mention, with their
  default and explanation, and any setting in your `.env` that ColdVault doesn't know
  (usually a typo). It reads only setting *names* from your `.env`, so your keys are
  never printed. `./env-check.sh --append` adds the new settings to the end of `.env`
  **commented out**: nothing changes until you uncomment one, and the next check comes
  back clean. A setting you don't have simply uses its default, so this is about
  knowing what's available, not fixing anything.
- Your `.env`, `docker-compose.override.yml`, index database (`data/`) and downloaded
  files (`downloads/`) are never touched by updates — they are git-ignored.
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

   The canary can also say **which bucket** the drive belongs in, so one ColdVault can
   feed several buckets with no clicking — e.g. one bucket per production:

   ```bash
   echo '{"name": "SSD01", "bucket": "my-archive-bucket"}' > /path/to/drive/coldvault.canary
   # or plain text, if you'd rather edit it by hand on set:
   printf 'SSD01\nbucket: my-archive-bucket\n' > /path/to/drive/coldvault.canary
   ```

   In plain text, `bucket:`, `prefix:` and `name:` lines (`=` works too) are read
   wherever they appear, the first other line is the label, and `#` lines are
   comments. A canary that names no bucket goes to the **active** bucket, exactly as
   before.

   If the named bucket can't be used — a typo, no access, or not on the allowlist
   below — the drive is **not uploaded at all** rather than quietly sent to the active
   bucket. The dashboard shows the reason under *Active drives*, the log gets one
   `ERROR` line; fix the canary and replug.

   Because the canary lives on removable media, you can limit which buckets it may
   pick with `COLDVAULT_CANARY_BUCKETS=my-archive-bucket,other-show` (empty = any bucket
   the credentials can reach). The allowlist applies to canaries only — a bucket you
   pick yourself for a manual upload isn't restricted by it.

2. Plug the drive in. The watcher polls the watch roots (Debian: `/media`,
   macOS: `/Volumes` via the container's `/media`) every `COLDVAULT_WATCH_INTERVAL`
   seconds, scanning up to two levels deep (so `/media/<user>/<label>` is covered).
   A drive **without** the canary is ignored entirely — the canary is what opts a
   drive in. The watcher is filesystem-agnostic: it reads files through normal
   paths, so any filesystem the host can mount and read works (ext4, XFS, btrfs,
   ZFS, exFAT, NTFS, FAT32, HFS+, APFS, …).

3. ColdVault detects the canary and uploads everything as
   `<COLDVAULT_PREFIX>/<label>/<relative path>`, e.g.
   `raw/movie-drive-01/Movie_Project/Shooting_Day_01/Camera_A/Card_001/A001_C001.RAW`.

4. Watch progress in **Sessions** (files, bytes, percentage, per-part speed for large
   files); every file lands in the **Index** with its checksum. Re-plugging the drive
   later uploads only new or changed files.

A canary drive triggers **one** upload per plug-in. Restarting the app while the drive
stays mounted does **not** re-upload it (the last session already completed cleanly, and
our udev mount is read-only so nothing can have changed) — unplug and replug to force a
re-scan. If a restart interrupts an upload, that session is marked `failed` and the
still-mounted drive is re-scanned on next start to resume it (already-verified files are
skipped).

OS junk (`.DS_Store`, `System Volume Information`, `.Trash-*`, resource forks etc.) is
excluded — configurable via `COLDVAULT_EXCLUDE`.

**Auto-upload is switched per machine** on the Dashboard: the **Auto-upload** line of
this machine, and the **Auto-upload** column of each [agent](#second-machine-agent).
With it off, a plugged-in canary drive is detected but **waits**: it shows ⏸ with
**Upload now** (the whole drive, with the canary's label and bucket — exactly what
auto-upload would have done) and **Pick files…** (opens the Manual upload tree at the
drive, to tick parts of it and preview first). A drive that was already uploaded offers
**Upload again**. The switches are stored in the database; `COLDVAULT_AUTO_UPLOAD` (default
`true`) is only the starting value for a machine never switched.

> ⚠️ Auto-upload sends **everything on the drive** to Deep Archive, which has a
> 180-day minimum storage charge per object (deleting earlier still bills the full
> 180 days). When testing, use a drive containing only a small folder — or disable
> auto-upload and use a manual upload instead.

### Auto-eject when the upload is done

With `COLDVAULT_EJECT_AFTER_UPLOAD=true`, a canary upload that finishes with **zero
failed files** marks its drive as ready to unmount, so you can pull it without
checking the UI first. A session with any failure is never flagged (the drive stays
mounted and the log says why), and nothing is flagged while another upload is running
from the same drive.

A container cannot unmount a host filesystem, so ColdVault only *publishes* the
request on `/api/eject/pending`. The host-side helper shipped with the udev package
(`coldvault-eject` + a 30-second systemd timer, installed by
`deploy/usb-automount/install-udev.sh`) polls it, unmounts, removes the empty mount
point and reports the outcome back — logged in the app under the `eject` category.
See [deploy/usb-automount/README.md](deploy/usb-automount/README.md).

On macOS, or without the udev package, the flag is still visible on
`/api/eject/pending` if you want to drive `diskutil eject` yourself.

### Hot-plug and mount propagation

For the watcher to see a drive plugged in **after** the container started, the
`/media` bind mount must propagate new host mounts into the container. Docker bind
mounts default to `rprivate`, which does **not** — so the provided compose files
mount `/media` with `propagation: rslave`:

```yaml
- type: bind
  source: /media          # /Volumes on the macOS variant
  target: /media
  read_only: true
  bind:
    propagation: rslave
```

On Linux this requires the host root to be a **shared** mount. systemd makes `/`
shared by default; if hot-plugged drives still don't appear, run once:

```bash
sudo mount --make-rshared /
```

Verify from inside the container after plugging a drive in:

```bash
docker compose exec coldvault ls /media/$USER
```

If your drive shows on the host but not there, propagation isn't reaching the
container. On **macOS**, Docker Desktop shares `/Volumes` through a VM, so hot-plug
visibility is best-effort — plug the drive in before `up`, or restart the container
after plugging in. (If Docker Desktop rejects the propagation setting, revert that
block to the simple `- /Volumes:/media:ro`.)

On a **headless Debian server** (no desktop), USB drives don't auto-mount at all.
An installable udev + systemd package that auto-mounts canary drives read-only under
`/media` on plug-in — so the workflow is truly hands-off — lives in
[`deploy/usb-automount/`](deploy/usb-automount/):

```bash
cd deploy/usb-automount && sudo ./install-udev.sh
```

It checks the propagation prerequisites above during install, and coexists with a
separate usb-to-nextcloud sync if you run one (they mount at different paths).

## Manual uploads

Any folder can be uploaded from the dashboard's **Manual upload** box, as long as it is
(a) bind-mounted into the container and (b) listed in `COLDVAULT_BROWSE_ROOTS`. With a
[second machine](#second-machine-agent), **Upload from** picks the machine whose folders
you see; the same steps then run there.

Every manual upload names its destination in **Upload to bucket**. The list offers the
active bucket (preselected), the `.env` default, the canary allowlist and every bucket
already in the index; **other bucket…** takes any name, which is checked for access
before anything is queued. The confirmation dialog repeats the destination. Browsing
into a folder that has a canary preselects that canary's bucket and fills in its label,
so re-running a drive by hand lands it where the watcher would have. Picking a bucket
here does not change the active bucket.

Because these mounts are specific to each machine, add them in a
**`docker-compose.override.yml`** (git-ignored, auto-merged by `docker compose up`, so
`git pull` never conflicts with your local mounts) rather than editing the committed
`docker-compose.yml`. Copy the template and edit:

```bash
cp docker-compose.override.example.yml docker-compose.override.yml
```

```yaml
# docker-compose.override.yml
services:
  coldvault:
    volumes:
      - /tank/files:/tank/files:ro
```

and in `.env`:

```
COLDVAULT_BROWSE_ROOTS=/tank/files
```

Then `docker compose up -d`. **Keep the same path on both sides** so paths typed in the
UI match — a mismatch (e.g. `/data/x:/data/y`) shows the root as *(not mounted)* in the
dashboard. The label field controls the S3 key prefix; empty defaults to the folder
name. The override can also redirect where downloads land — see the template.

(On macOS the override is not auto-loaded because the run uses `-f
docker-compose.mac.yml`; add `-f docker-compose.override.yml` explicitly, or use the
commented `testdata` mount in `docker-compose.mac.yml`.)

The box shows a **tree** of every configured upload root (the watch dirs plus
`COLDVAULT_BROWSE_ROOTS`), so you don't have to remember paths. Each root is checked
live inside the container: ones that are configured but missing show as *(not mounted)*
and ones present but without permission as *(not readable)* — a quick way to confirm a
new volume mount actually landed. ▸ opens a folder in place (its subfolders and files,
with sizes); names that are never uploaded (`COLDVAULT_EXCLUDE`, the canary file) are
shown greyed as *excluded*.

### Uploading a whole folder or just parts of it

Every folder and file in the tree has a checkbox:

- **Tick specific folders and/or files** and only those are uploaded. A ticked folder
  takes everything inside it — its contents show ticked and greyed. Ticks stay while you
  open and close other folders, so one upload can gather items from all over the tree;
  **Clear selection** empties it.
- **Tick nothing** and **Start upload** archives the whole *open* folder — the folder
  whose name you last clicked (shown in the path field), or a path typed there (**Open**
  jumps the tree to it).

### Preview: what would actually upload

**Preview** lists the selection on the machine that has it — with the same rules the
upload uses — and checks every file against the index, without reading any file or
calling AWS. It shows the S3 key each file would get and sorts them into:

| Group | Uploads? |
|---|---|
| **New** | yes |
| **Changed since it was archived** (different size) — replaces the archived copy | yes |
| **Not archived yet** — an earlier upload failed or didn't finish | yes |
| **Same size, different date** — read and compared during the upload | only if the content differs |
| **New here, but an object of the same size is already archived** | only if the content differs (duplicate check) |
| **Being uploaded right now** by another session | no — skipped if that one archives the same content |
| **Already archived and unchanged** | no |
| **Other content already archived under this name from another machine** | no — refused; use another label |
| **Excluded by name** | no |

with totals at the top (e.g. *Uploads 212 files · 1.4 TB*). Changing the selection,
label or bucket marks the preview out of date; the confirmation of **Start upload**
repeats the totals of a current preview. A preview covers up to 200,000 files — preview
a bigger drive in parts.

Keys mirror the structure under the **common parent** of your selection, so selecting a
folder produces exactly the same keys as a whole-folder upload of it, and picking two
day folders keeps them both under the project (`Project/Day01/…`, `Project/Day02/…`).
Already-verified and duplicate content is skipped as usual.

**Prefix and label** decide where the keys start:

| Prefix | Label | Ticked folder | Keys |
|---|---|---|---|
| — | — | `RAID01` | `RAID01/…` |
| `Hotel Example` | — | `RAID01` | `Hotel Example/RAID01/…` |
| `Hotel Example/RAW` | — | `RAID01` | `Hotel Example/RAW/RAID01/…` |
| `Hotel Example` | `Disk01` | `RAID01` | `Hotel Example/Disk01/…` |

The **prefix** (e.g. the film) goes in front; the **label** replaces the folder's own
name, and `COLDVAULT_PREFIX` (if set) still comes first. The prefix box suggests the
prefixes used before in the chosen bucket (newest first) and the folders already at the
top of that bucket — or type a new one. The line under the fields shows where the keys
will start and what the prefix means: **new prefix**, **adds to the existing "…"** (with
what's there now), or a warning when it differs from an existing folder only in
upper/lower case (S3 would make it a second folder). With **no prefix** the line warns
that the files go to the top of the bucket under a folder's name, and so does the
confirmation. The box starts empty each time, so one film can't quietly inherit the
previous film's prefix.
**Start upload** counts the selection on the machine that has it and asks in a
confirmation window with the real numbers (*48,211 files · 50.2 TB*, or a current
preview's totals), the destination, and **what goes where**: every ticked folder or
file with the exact place it lands (`/mnt/raid/RAID01/Day01 → s3://bucket/Hotel
Example/RAID01/Day01/…`). Long lists scroll; nothing is cut off.

### Stopping an upload

**Sessions** → **Stop** on a queued or running session, on this machine or an agent. A
queued session never starts; a running one starts no new file. A large file in progress
is abandoned **between parts** and its multipart upload aborted, so nothing is left
half-uploaded (and billed) in S3; a small file in flight finishes. Everything already
verified stays — run the same upload again later and it carries on, skipping those.
A canary drive whose upload was stopped isn't restarted automatically after a restart;
use **Upload again** on the Dashboard.

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

Before starting, ColdVault checks the selection fits the destination filesystem
(keeping `COLDVAULT_DOWNLOAD_MIN_FREE_MB` free, default 200). Files already on disk at
the destination don't count (they'd be skipped anyway). If the rest won't fit, it asks
whether to **download only the files that fit** (it keeps each file whose size still
fits the remaining space and skips the ones that don't) or **cancel the whole
download** — nothing is written until you choose.

The restored list tags each object with its **local state** — *on disk*, *deleted*
(was downloaded, since removed), or *not downloaded* — and marks restores whose window
has **expired** (no longer downloadable; re-request them in the Index tab). **Check
local files** re-scans the destination and flags anything you've deleted, so a file
removed from disk stops showing as downloaded. **Select not-downloaded** picks only the
objects you don't already have.

Large objects are fetched with **parallel ranged GETs**
(`COLDVAULT_DOWNLOAD_PART_WORKERS`, default 4) written directly into the target file,
mirroring the parallel multipart upload path. Progress, per-part speed and per-file
speed appear in the session table and the log.

> The download directory must be mounted **read-write** in docker-compose (unlike the
> source volumes, which are read-only). The default maps `./downloads`; point it
> anywhere you like, e.g. `- /tank/restores:/downloads`. Downloads are refused
> outside this directory.

### Local files not in the index

Downloaded data can outlive the index that described it — most often after
**Clear local index** during a bucket migration, which drops that bucket's download
records while the files stay on disk. **Scan download folder** finds them: every file
under the download directory that no index row points at, newest-largest first.

Because a downloaded file's path under the destination *is* its S3 key, ColdVault
looks each one up across **every** indexed bucket and labels it:

| Label | Meaning |
|-------|---------|
| `in <bucket>` | Still archived at the same size — safe to delete locally and re-restore later. |
| `size differs` | Archived under that key, but not these bytes. Look before you delete. |
| **`not archived`** | No indexed bucket has this key — the local copy may be the **only** one left. |

**Select still-archived** ticks only the safely re-downloadable ones. **Delete
selected** removes the **local** copy and nothing else — S3 is never touched. A file
that isn't archived anywhere is refused by the API unless explicitly forced, and the
UI makes you type `DELETE` for those; paths outside the download folder are always
refused. Emptied directories are tidied up, and every deletion is logged.

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

## Offload manifest cross-check

Camera offload tools (Silverstack, YoYotta, Pomfort, ShotPut, …) write **checksum
manifests** next to the media: a Silverstack-style **CSV**, and/or **ASC MHL** files
(`ascmhl/*.mhl`, one per card — the industry standard, covering every file including
sound). If ColdVault finds any of these anywhere in a folder it's uploading, it uses them
as an **independent integrity check**: each file is matched to the manifest by name, and
the manifest's recorded hash (XXH64, XXH3, MD5, SHA-1 or SHA-256) is recomputed **in the
same read pass** as the upload SHA-256 and compared. This catches corruption that happened *between* the
offload and the archive — something S3's own checksums can't see, because they only
prove the bytes ColdVault read arrived intact.

Results appear as badges in the **Index** — `csv✓` (matches the manifest), `csv✗`
(mismatch) — and are filterable (**manifest: mismatch / ok / not in manifest**), so you
can pull up every mismatch across a shoot in one click. Mismatches are logged as errors
under the `manifest` category. Matching is by filename (and name-without-extension), so
a manifest row named `B001_F001` still matches `B001_F001.cine`.

Enabled by default (`COLDVAULT_MANIFEST_CHECK=true`); set the manifest file extensions
with `COLDVAULT_MANIFEST_EXTS` (default `.csv,.mhl`). Manifests can sit anywhere in the
uploaded tree — the scan is recursive. Files not listed in any manifest, and uploads from
folders without one, simply carry no badge. Every upload logs a one-line summary
(`manifest check: N ok, M mismatch, …`, or that no manifest was found), and on startup
ColdVault logs whether the `xxhash` library is installed — XXH64/XXH3 checks need it, and
`/health` reports it under `manifest_check`.

### Verifying against a manifest later

Already archived something without a manifest check (or got the manifest afterwards)?
The **Index** tab's **Verify against offload manifest** box handles it:

- **Drag & drop** one or more `.csv` / `.mhl` files from your computer, **or**
- **Scan a server folder** (e.g. `/media/SSD01` while the drive is still mounted) to pick
  up every manifest inside it at once.

Each listed file is cross-checked against the archive — **present** (name and size match),
**size mismatch**, or **not archived** — and the manifest hash is **attached** to the
archived object (`csv·` badge), so restoring + downloading it later re-verifies the bytes.
Use the optional **scope** field (e.g. `SD01_20261005_MON`) when the same filename exists
in several shoots.

Tick **deep verify** to go further while the source drive is still mounted: ColdVault
re-reads each source file in the background, computing SHA-256 and the manifest hash in one
pass. If the SHA-256 equals the archived object's, the local bytes *are* the archived bytes,
so the manifest comparison is a true byte-level verification of the archive — without
restoring anything from Deep Archive. Results become `csv✓` / `csv✗`; files whose source
changed or is no longer mounted are reported, not guessed.

The **bucket audit** re-surfaces this: it reports how many objects are manifest-verified
vs mismatched and flags any mismatch as an audit finding, so a bad file shows up in every
audit and email report, not only when it was first uploaded. (Deep Archive objects can't
be re-hashed without a restore, so the audit re-checks the result recorded at upload
rather than re-reading the bytes.)

When you actually **download** a restored object, ColdVault does the *byte-level*
re-verify: it recomputes the manifest hash from the restored bytes (in the same read pass
as the download's SHA-256 check) and compares it to the offload value — the strongest
confirmation that what came back from Deep Archive still matches what the DIT recorded.
Results show as `csv✓` / `csv✗` in the Downloaded-files table.

## Tree view

The **Tree** tab shows the archive as a folder tree, expanded lazily so it stays fast
on large buckets. The top level lists **every bucket** in the index (plus the active
one), each with its object count and size — click a bucket to open it; the active one
opens on first visit. Each folder reports its whole subtree's file count and size.

Open folders stay open while you work — ticking, scheduling or refreshing never
collapses the tree or moves you off your place.

Tick a **folder** to act on everything inside it, or tick individual files, then:

- **Restore selected** — requests a Bulk/Standard restore for every object under the
  selection (the prefix is expanded server-side, so a folder of thousands of clips is
  one click, not thousands of checkboxes). The selection may span buckets.
- **Set / Clear expiry** — schedules a planned deletion date for the selection, the
  same mechanism as the Retention tab. Scheduling never deletes: what you schedule is
  listed under **Retention → Expiry schedule**, to action deliberately once it's due.
  Works across buckets too.
- **Delete selected** — runs the same guarded flow as the Retention tab: a preview of
  what it covers, the cost warnings, a confirmation, and typing the bucket name. It
  takes **one bucket at a time**, so untick the others first.

### Exporting the tree

Pick the bucket next to **Export**; the **Excel**, **PDF** and **CSV** buttons export that whole bucket's tree — every folder
with its subtree's file count and size, plus the grand totals — stamped with the
bucket name and the local date and time, so a filed or printed copy is
self-describing. The export covers the bucket, not just what you've ticked.

| Format | Contents |
|--------|----------|
| **Excel** (`.xlsx`) | *Tree* sheet: folders indented by depth, with files, exact bytes and a human size, ending in a TOTAL row. *Files* sheet: every object (key, size, status, storage class, uploaded/verified, SHA-256, manifest state, expiry) with filters on. |
| **PDF** | Paginated, indented report with a header block and per-page footer — for printing, filing or sending to a client. |
| **CSV** | Meta block, the folder roll-up, totals, then every object. UTF-8 with a BOM so Excel opens it cleanly. |

Exports read the local index only — no AWS calls, nothing is restored, and Deep
Archive objects stay cold.

## Retention: expiry and deletion

The **Retention** tab manages the end of an archive's life. Deleting is the only
irreversible thing ColdVault does, so it is deliberately a three-step flow.

The **Bucket** picker at the top defaults to **all buckets**, so an expiry or deletion
made from the Tree shows up here whichever bucket it's in. The expiry schedule and the
deletion log both have a Bucket column, and the permission panel has one line per bucket.
Preview and expiry work across buckets too. **Deleting always takes one bucket**: if a
preview, or what's due, spans several buckets, pick the one you mean in the picker first.

1. **Select and preview.** Enter a key prefix — a folder in the archive, e.g.
   `SSD01/SD01_20261005_MON/CAMERA_FILES/` — and press **Preview**. You get the exact
   object count and total size, a sample of the keys, and two cost warnings where they
   apply:
   - **Minimum storage duration.** Deep Archive bills a minimum of **180 days** per
     object (Glacier 90, IA 30). Deleting sooner does *not* save money — you are still
     charged the remaining days. The preview says how many objects are affected and for
     how many more days.
   - **Bucket versioning.** If versioning is enabled, a delete only writes a delete
     marker: the object versions, and their cost, remain until purged.
2. **Schedule expiry (optional).** Set a planned deletion date on the previewed set, or
   clear it. **Nothing is ever deleted automatically.** The **Expiry schedule** table
   lists everything you've scheduled with a countdown to its date, and a per-row
   **clear** to unschedule one object; switch it to *due now only* to see just what has
   come due and is waiting for you to action.
3. **Delete.** Either **Delete previewed…** or **Delete all due…**. Both require
   confirming the summary and then typing the bucket name. An empty match is refused, so
   a mistyped prefix can't turn into a mass delete.

Every deletion is **audited twice**: a tombstone row in the `deletions` table recording
the key, size, SHA-256, manifest hash, upload date, reason, mode (manual/expiry) and how
many days short of the minimum duration it was — this outlives the index entry, so the
archive can always answer *what was removed and why* — plus full logging under the
`retention` category. Deletions that fail (e.g. AccessDenied) are reported, leave the
index row intact, and write no tombstone.

### Can ColdVault delete at all?

The Retention tab **tells you**, rather than leaving it to your memory of the IAM
policy. There's no read-only way to ask S3 this, so ColdVault probes it: a
`delete-object` on a random key under a reserved prefix. Nothing of yours is named —
the key doesn't exist, and S3's delete is idempotent, so it removes nothing. The
result is **probed once and remembered**; it is re-probed only when you press
**Re-check** (an IAM policy doesn't change between page loads). Set
`COLDVAULT_DELETE_PROBE=false` if you'd rather ColdVault never issue a delete call at
all — the banner then reads "unknown" and the delete buttons stay disabled.

The banner shows one of:

- 🔒 **Append-only** — the IAM user has no `s3:DeleteObject`, so nothing in ColdVault can
  remove your archive. The delete buttons are disabled. Scheduling expiry still works
  (it only flags what's due).
- ⚠ **This IAM user CAN permanently delete** — deletion is live and irreversible.

To make ColdVault strictly append-only, simply **don't grant `s3:DeleteObject`** — every
other feature keeps working.

### Deleting only when you unlock it (recommended)

The better arrangement is **both**: an everyday user that can never delete, plus a
separate **delete role** that ColdVault unlocks only for a deletion you've confirmed —
with the 6-digit code from your authenticator app — for 15 minutes, then forgets. A
leaked `.env` can't delete anything, and every deletion is visible in CloudTrail as an
MFA-verified role session. The step-by-step setup is in
[AWS users and permissions](#aws-users-and-permissions); it ends with, in `.env`:

```
COLDVAULT_DELETE_ROLE_ARN=arn:aws:iam::123456789012:role/ColdVaultDelete
COLDVAULT_DELETE_MFA_SERIAL=arn:aws:iam::123456789012:mfa/coldvault-master
```

The Retention tab then shows 🔒 *append-only* for the everyday user and 🔐 *deleting
goes through the delete role*; the delete buttons work, and each deletion asks for the
code as its last step. **Test delete role** proves the whole chain (unlock + permission)
without removing anything.

## The earliest charge-free expiry date

Deep Archive bills every object for at least **180 days** from upload (Glacier 90, IA
30) — deleting earlier still charges the remaining days. So for any selection there's
an earliest date on which deleting costs nothing extra: the **newest** object's upload
date plus the minimum. ColdVault shows it wherever you set an expiry — after
**Preview** in Retention, and next to the date in the Tree (*charge-free from
2027-05-03 · use*). If you schedule an earlier date, it says how many objects would
still be inside their minimum on that day and by how much, and asks before setting it.
The Expiry schedule's *Owed at expiry* column shows the same for what's scheduled.

## Integrity audit

Uploads are checksum-verified when they happen, but for cold storage it's worth
periodically confirming the archive is still intact. **Audit bucket** (dashboard,
next to Import) reconciles the index against the actual bucket contents:

- Lists every object in the active bucket (**paginated**, so it works on buckets with
  far more than 1000 objects) and compares it to what the index expects.
- Flags any object the index believes is archived but that is **missing** from the
  bucket (deleted, lifecycle-expired, or a write that never landed), any **size
  mismatch**, and any object not in the expected storage class (**class drift**).
- Imports objects found in the bucket but not yet in the index (same as
  *Import bucket → index*).

Results are summarised on the dashboard, each flagged object gets a badge in the
**Index** tab (`missing` / `size_mismatch` / `class_drift`), and everything is written
to the log under the `audit` category — a `missing` object is logged at ERROR level so
it stands out. Re-running the audit refreshes all badges, so once you've fixed
something (e.g. re-uploaded a missing file) the flag clears. The audit reads object
metadata only (`list-objects-v2`); it does not restore or download anything, so it is
cheap to run and safe on Deep Archive data.

## Email notifications (Resend)

ColdVault can email you a report after each automatic (canary) upload — what was
archived, the current bucket size, and the audit result — using
[Resend](https://resend.com). Configure in `.env`:

```
COLDVAULT_NOTIFY=true
RESEND_API_KEY=re_...
COLDVAULT_EMAIL_FROM=ColdVault <coldvault@your-verified-domain.com>
COLDVAULT_EMAIL_TO=you@example.com          # comma-separate for several
```

With `COLDVAULT_NOTIFY=true`, every watcher/canary upload is followed by a bucket audit
and a report email (subject notes whether the audit was **OK** or **found issues**, and
issues are listed in the body). Manual uploads don't email.

The **Email notifications** box on the dashboard also lets you **Email an audit report
on demand** (runs an audit now and mails it) and **Send a test email** to verify the
setup. The sender address must be on a domain verified in your Resend account (or use
`onboarding@resend.dev` for a quick test).

## Backing up ColdVault itself

S3 holds your files, but the **index** — checksums verified at upload, offload-manifest
results, sessions, restore history, deletion records, settings — exists only in
ColdVault's SQLite database. S3 can't give it back. **Settings → Backup** looks after it.

Don't point rsync, borg or restic at the live `data/coldvault.db`: a copy taken while
ColdVault is writing can be a half-written, corrupt file. ColdVault instead writes its
own consistent snapshot (SQLite's online backup API, then `PRAGMA integrity_check`) to
the backup folder, `./backups` on the host (mounted at `/backups`):

```
backups/
  latest/                 newest complete snapshot — back THIS up
    coldvault.db          the database, integrity-checked
    files.csv             the index as plain text (bucket, key, size, sha256, …)
    deletions.csv         every deletion tombstone
    coldvault.log*        the logs
    config.json           effective settings, with credentials left out
    backup.json           manifest: time, version, row counts, sha256 of each file
  history/<date_time>/    the last N snapshots (hard links, cheap on space)
```

A snapshot is built in a hidden folder and swapped in only once it is complete and
checked, so `latest/` is never half-written. A failed backup leaves the previous
`latest/` in place. Files stay uncompressed so borg and restic can deduplicate
between runs (gzip would defeat that). The CSVs mean the index is readable even
without SQLite.

**When it runs.** Daily at 03:00 local time by default, keeping 7 snapshots in
`history/`. Change both in Settings (saved in the database, overriding `.env`), or set
`COLDVAULT_BACKUP_AT` / `COLDVAULT_BACKUP_KEEP`. An empty time means on demand only. If
the container was down at the scheduled time, the backup runs when it comes back; a
failed backup is retried an hour later. **Back up now** takes one immediately.

**Hooking up your tool.** The safest way is to ask ColdVault for a fresh snapshot right
before your tool runs. `POST /api/backup` returns only once the snapshot is finished,
and returns HTTP 500 if it failed, so `curl -f` stops the chain:

```bash
# e.g. in a host crontab
curl -fsS -X POST http://localhost:9999/api/backup \
  && restic -r /mnt/nas/restic backup ~/coldvault/backups/latest
curl -fsS -X POST http://localhost:9999/api/backup \
  && borg create /mnt/nas/borg::coldvault-{now} ~/coldvault/backups/latest
curl -fsS -X POST http://localhost:9999/api/backup \
  && rsync -a --delete ~/coldvault/backups/ nas:/backups/coldvault/
```

Alternatively, schedule your tool for some time after the daily backup. Files are
written by the container (as root) but are world-readable, so your tool doesn't need
root to read them.

**Not included:** your `.env`. It holds the AWS keys and the Resend key, which
ColdVault deliberately never copies. It lives on the host, so add it to your tool's
backup set (encrypted, ideally).

**Restoring:** stop ColdVault, copy `backups/latest/coldvault.db` over
`data/coldvault.db`, start ColdVault. Then bring it up to date with S3:
- **Import bucket → index** picks up anything uploaded after the snapshot. Those objects
  come back as `remote`, without their upload-time checksums.
- **Audit bucket** flags anything the snapshot lists that is no longer there.

`/health` reports a `backup` block and turns to `attention` if the last backup failed,
or if a scheduled backup hasn't succeeded in two days.

## Second machine (agent)

A second computer — typically a Raspberry Pi, or another machine on the same network —
can work alongside the main ColdVault. The main one is the **master**: it keeps the one
database, serves the web UI, and does everything it does today. The second one is an
**agent**: it does only the work that has to happen where its disks are.

- **Canary drives plugged into the agent** upload automatically, exactly like on the
  master — same canary file, same bucket choice, same one-upload-per-plug-in rule — or,
  with its **Auto-upload** switched off on the Dashboard, wait for **Upload now** /
  **Pick files…** ([per machine](#the-canary-workflow)). The Dashboard's **Agents** box
  shows each agent, online or not, what it's doing, and the drives it sees.
- **Manual uploads**: pick the agent in **Upload from**, tick files and folders in its
  tree, **Preview**, start the upload. The agent reads its own disk and sends the files
  straight to S3. To make another of its folders available (e.g. a RAID at `/mnt/raid`),
  mount it read-only in that machine's `docker-compose.override.yml` and list it in its
  `.env` as `COLDVAULT_BROWSE_ROOTS` — see [Manual uploads](#manual-uploads).
- **Downloads**: pick the agent in **Download to** on the Downloads tab. Restored files
  are fetched straight from S3 onto the agent's disk and checksum-verified there. "Local
  file", **Check local files** and the leftover-file scan then show that machine.
- **Restores, the index, Tree, Retention, deletions, email reports and backups** stay on
  the master. Restores are requested there as before; any restored object can then be
  downloaded to either machine.

Both machines upload **at the same time**, each from its own drives. Every file's S3 key
is reserved just before it uploads, so two machines never write the same object at once:
the second waits, then skips if the first archived identical content. If the second has
*different* content under the same key — almost always two drives given the same label
— it is **refused** with a clear error rather than replacing the other machine's object
(the replaced one would still be billed its full 180 days). Rename one of the labels.
They share your internet upload speed, so two at once each go about half as fast.

**The agent has no database.** Every record it makes goes to the master at once. When
the master can't be reached, the agent **stops**: a file already uploading finishes and
its record waits, nothing new starts, and everything resumes when the master is back. A
master restart doesn't disturb an agent's session. An agent that restarts, or stays
silent for `COLDVAULT_AGENT_FAIL_MINUTES` (10), has its interrupted sessions marked
failed — re-run them; verified files are skipped. `/health` turns to `attention` while an
offline agent still owes work.

### Setting it up

**1. On the master** — give it a name and a shared secret, in `.env`:

```bash
openssl rand -hex 32        # a long random token
```

```
COLDVAULT_NAME=master
COLDVAULT_AGENT_TOKEN=<the token>
```

then `docker compose up -d`. Without a token, agents are refused.

**2. An AWS user for the agent.** Give the agent its own IAM user that can upload and
download but **not delete** — deleting stays possible only on the master.
[deploy/iam/](deploy/iam/README.md) writes its policy (`agent-user.json`) along with
the master's and walks through creating it. (Upload rights technically include
overwriting; the key reservation above is what stops machines overwriting each other.)

**3. On the agent** — a 64-bit OS (the AWS CLI has no 32-bit ARM build; on a Pi use
Raspberry Pi OS 64-bit), Docker with the compose plugin, and the repository:

```bash
git clone https://github.com/tadekus/coldvault.git && cd coldvault
cp .env.agent.example .env      # then edit it
docker compose -f docker-compose.agent.yml up -d --build
```

In `.env`, set `COLDVAULT_NAME` (how it appears in the UI, e.g. `pi`),
`COLDVAULT_MASTER_URL` (e.g. `http://<master-LAN-IP>:9999` — the master's LAN IP is the
safest choice, since `.local` names often don't resolve inside Docker), the same
`COLDVAULT_AGENT_TOKEN`, and the agent's AWS keys. The bucket, key prefix, storage class,
canary file name and duplicate/manifest checks come from the master, so they can't drift
apart. For canary drives, install the [USB automount package](deploy/usb-automount/README.md)
on the agent too (a headless Pi doesn't auto-mount drives on its own); with `COLDVAULT_EJECT_AFTER_UPLOAD=true` there, its eject helper
unmounts the agent's drives after a clean upload.

Within a few seconds the agent appears on the master's Dashboard under **Agents**. The
agent itself serves only a small status page on its port (`http://pi:9999`) — connection
to the master, current activity, drives — plus `/health` for monitoring.

**Updating:** update both machines to the same version (`git pull` and the same
`up -d --build` on each). If their protocol differs, the master refuses the agent with a
message saying so, and the Dashboard marks a version mismatch.

### On a Raspberry Pi

- **SD card or SSD:** `.env.agent.example` uploads files up to 4 GB
  (`COLDVAULT_MULTIPART_THRESHOLD_MB=4096`) straight from the drive, so nothing is written
  to the Pi's own disk for them; only bigger files are staged there in
  `COLDVAULT_PART_SIZE_MB` pieces (about 512 MB at a time). Running from the SD card works,
  but if you often archive files over 4 GB, a USB SSD or a "high endurance" card lasts
  longer. A worn-out card loses no records — the agent keeps none. Downloads land in
  `./downloads`, so point that at a bigger disk in `docker-compose.agent.yml` if the card
  is small.
- A Pi 4 has no hardware SHA-256, so checksumming is slower than on a desktop; a Pi 5
  does. On most home connections the upload speed is the limit anyway.

**Security:** agents authenticate with the token, and each may change only its own
sessions' records. The web UI itself still has no login, so keep both machines on your
own network or a VPN such as Tailscale, never open to the internet.

## Health / monitoring endpoint

`GET /health` returns a JSON snapshot of upload, restore and download activity plus
integrity signals, for uptime monitors and dashboards. It reads only the local index
and in-memory state — **no AWS calls** — so it's safe to poll frequently.

```bash
curl -s http://localhost:9999/health | jq
```

Top-level `status` is **`ok`** (idle, healthy), **`busy`** (an upload/download is running
or queued), or **`attention`** (failed uploads, manifest mismatches, audit issues, or a
failed/stale local backup need looking at). Options:

- `?strict=1` — return HTTP **503** when `status` is `attention` (so a monitor alerts),
  otherwise **200**.
- `?check_aws=1` — also run a live `sts get-caller-identity` + `head-bucket` and add an
  `aws` block (off by default, since it costs a round-trip).

Body fields: `version`, `time`, `uptime_seconds`, `bucket`; `objects` (verified / remote
/ failed counts); `uploads` (`running`, `current_session`, `queued`, `active` progress
with `files_done/total`, `bytes_done/total`, `percent`, and `last_session` summary);
`restores` (`in_progress`, `completed`, `failed`, `last_checked`); `downloads` (`running`,
`current_session`, `queued`); and `integrity` (`failed_uploads`, `manifest_mismatch`,
`audit_missing`, `audit_size_mismatch`, `audit_class_drift`). Integrity counts are for
the active bucket. `backup` has `scheduled`, `last_ok`, `last_at`, `last_error` and
`stale`; a failed or stale backup also sets `status` to `attention`. `agents` lists each
second machine with `online`, `last_seen`, `version` and `pending` sessions; an offline
agent that still owes work sets `status` to `attention`.

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
- AWS permissions: see [AWS users and permissions](#aws-users-and-permissions). In
  short, the everyday user needs `s3:ListAllMyBuckets`, `s3:ListBucket` and
  `s3:GetBucketVersioning` on the buckets, and `s3:PutObject`, `s3:GetObject`,
  `s3:RestoreObject`, `s3:AbortMultipartUpload` on their objects — and should **not**
  have `s3:DeleteObject`; deleting goes through the delete role.
- `COLDVAULT_BUCKET` in `.env` is the initial bucket. You can also click
  **List buckets** on the dashboard and pick one there — that choice is persisted in
  `./data` and overrides the `.env` value until you pick another.
- **Migrating/deleting a bucket?** Use **Clear local index** (dashboard → Connection) to
  remove that bucket's stale index records — or the whole database — with a typed
  confirmation. It only clears the local index; it never deletes anything from S3.
- The index/DB and logs persist in `./data` (bind-mounted to `/data`). The database
  (`data/coldvault.db`) is your file inventory — worth backing up occasionally.
- The web UI has no authentication — bind it to localhost or your LAN only
  (e.g. `127.0.0.1:9999:9999` in docker-compose) and don't expose it to the internet.
- Timestamps are shown in **local time**, including values that come from S3 (restore
  expiry dates and imported objects' last-modified), which are converted from UTC/GMT on
  arrival. On Debian the container inherits the host timezone via the `/etc/localtime`
  mount; set `TZ` in `.env` (e.g. `Europe/Prague`) to force one, which is also how to set
  it on macOS. Rows written before v1.9.0 keep their original UTC/GMT values.
- Sizes use **decimal (SI) units** — 1 TB = 1000⁴ bytes — the same convention as DIT
  offload reports, drive capacities and Finder, so ColdVault's totals match what your
  offload tool says. Hover a size in the Index to see the exact byte count. (Note that a
  tool reporting binary "TiB" will show a smaller number for the same data: 1.65 TB =
  1.5 TiB.)
