# Changelog

All notable changes to ColdVault. Versions follow `MAJOR.MINOR.PATCH`
(PATCH = fixes/tweaks, MINOR = features, MAJOR = breaking). The running version is
in [`app/version.py`](app/version.py) and shown in the web UI header.

## 1.22.0

- **Prefix suggestions.** The manual-upload prefix box lists:
  - prefixes used before in the chosen bucket, newest first, with how often and when;
  - the folders already at the top of that bucket, with file count and size.

  You can still type a new one. Sessions now record the prefix they were started
  with; the database adds the column on its own.
- The line under the fields explains the prefix:
  - **new prefix**;
  - **adds to the existing "…"**, with what's there now;
  - a warning when it differs from an existing folder only in upper/lower case.
- **Warning without a prefix.** Shown under the fields, with which folder name the keys
  would start with, and again at the top of the confirmation.
- The prefix box no longer remembers the last value; recent prefixes are in its list.
  This way one film can't quietly inherit the previous one's prefix.

## 1.21.0

- **Stop an upload:** a **Stop** button in Sessions, for this machine and for agents.
  - A queued session never starts; a running one starts no new file.
  - A large file in progress is abandoned between parts and its multipart upload is
    aborted, so nothing is left half-uploaded or billed in S3.
  - Verified files stay, and re-running the upload skips them.
  - The status is `stopped`, and it stays so even if the machine reports back late.
  - No report email or eject follows a stopped session.
  - A canary drive whose upload was stopped isn't restarted after a restart.
- **Prefix for manual uploads,** for example the film's name, in front of the folder
  name: `Hotel Example/RAID01/…`. It may contain `/`, and `.`/`..` are refused.
  - A line under the field shows where the keys will start. The browser remembers
    the prefix.
  - The label still replaces the folder's own name.
- **Start upload's confirmation shows the real numbers.** It counts the files and
  bytes on the machine that has them, or uses a current preview's totals, and shows
  the full destination including the prefix.
- Preview: a file whose earlier upload was stopped or failed says why.

## 1.20.0

- **Manual upload: a tree with a preview.**
  - The Manual upload box shows each machine's upload roots as a tree. ▸ opens
    folders in place, and checkboxes tick files and folders; a ticked folder takes
    everything inside it. Names that are never uploaded are shown greyed as *excluded*.
  - **Preview** lists the selection on the machine that has it, with the uploader's
    own rules, and checks every file against the index. It shows each file's S3 key in
    groups: new, changed (replaces the archived copy), not archived yet, same size but
    a different date (compared during upload), possible duplicate, being uploaded
    now, already archived (skipped), refused (other content from another machine
    under the same name), excluded. Totals are shown at the top.
  - No file is read and AWS isn't called. Changing the selection, label or bucket
    marks the preview out of date; Start upload's confirmation repeats its totals.
  - Works for the master and for agents. An agent must run 1.20.0 for Preview.
- **Auto-upload per machine.**
  - An on/off switch for this machine (Dashboard → Auto-upload) and for each agent
    (Agents → Auto-upload column), stored in the database. `COLDVAULT_AUTO_UPLOAD`
    is only the starting value.
  - With it off, a plugged-in canary drive waits (⏸) and offers **Upload now**, which
    uploads the whole drive with its canary's label and bucket, and **Pick files…**,
    which opens the tree at the drive. An uploaded drive offers **Upload again**.
- Internal: the uploader's file scan is shared with the preview, so both always agree
  on what's included.

## 1.19.4

- **Fix: a delete from the Tree with the delete role could fail silently.** A
  refused delete showed its reason only as small grey text under the toolbar and
  wrote nothing to the log. Now every refused or failed delete, in the Tree and on
  Retention:
  - pops up "Nothing was deleted" with the reason, and shows it in red;
  - writes a `delete refused …` or `delete role NOT unlocked …` line to the log
    (never the code itself).
- The authenticator code may be typed as the app shows it, `123 456` (spaces or
  dashes are ignored). A code that isn't 6 digits is asked for again instead of
  being sent.
- The bucket-name prompt says that the code is asked next. A code typed into the
  bucket-name box by mistake is recognised and explained.
- The Tree's Delete waits for the bucket's permission check, so clicking it right
  after ticking a file still asks for the code.

## 1.19.3

- `.env.agent.example`: `COLDVAULT_MULTIPART_THRESHOLD_MB` is now **4096** (was 512).
  Files up to 4 GB upload straight from the drive, so an agent writes nothing to its
  own disk for them. This makes running a Raspberry Pi from its SD card practical.
  Existing agents keep their own `.env` value; change it there to get the same.
  The master's default is unchanged.
- README "On a Raspberry Pi" updated to match.

## 1.19.2

- Removed personal details from files published to GitHub. Example values
  now use placeholders in place of the real master computer name, user name and
  paths, LAN IP, bucket names and drive name: `master-host`, `<master-LAN-IP>`,
  `/home/me`, `my-archive-bucket`, `SSD01`. Affected files: `.env.example`,
  `.env.agent.example`, the compose override example, README, and UI hints.
  `COLDVAULT_BROWSE_ROOTS` in `.env.example` is now empty by default. Your own
  `.env` is unaffected; it was never committed.

## 1.19.1

- README: **AWS users and permissions**, a full guide to the identities ColdVault
  should run with. It covers the everyday user (can never delete, enforced by an
  explicit Deny), the MFA-unlocked delete role, and agent users, and why a role
  is safer than a second user with delete keys.
  - Checking which identity the running ColdVault uses (one read-only command),
    and when to keep an existing user versus create a new one (not if it's also
    your console login), with a warning about root keys.
  - Generating the policies without placeholders, the AWS console click path
    and the equivalent CLI commands, the `.env` lines, testing with Re-check and
    Test delete role, adding a bucket later, and a troubleshooting table.
- `deploy/iam/README.md` now just describes the generator and points to that guide,
  so the two can't drift apart. The Notes section's permission list is updated
  for the delete role.

## 1.19.0

- **The earliest charge-free expiry date.** Deep Archive bills every object for at
  least 180 days from upload (Glacier 90, IA 30), so for any selection there's an
  earliest date on which deleting costs nothing extra: the newest object's upload
  plus the minimum. It's shown after **Preview** in Retention and next to the
  Tree's date picker (*charge-free from 2027-05-04 · use*), with one click to use
  it. Scheduling an earlier date first says how many objects would still be inside
  their minimum on that day and by how much, and asks before setting it. The
  Expiry schedule's column is now **Owed at expiry**: days still billed if deleted
  on the planned date, not today.
- **Deleting only when you unlock it: a separate delete role.** With
  `COLDVAULT_DELETE_ROLE_ARN` (and `COLDVAULT_DELETE_MFA_SERIAL`) set, the everyday
  IAM user never needs delete rights. For each deletion, after the usual preview,
  confirmation and typed bucket name, ColdVault asks for the 6-digit code from your
  authenticator app. It then assumes the role for 15 minutes, deletes, and forgets
  the credentials. They are passed only to that one `aws` call's environment,
  never logged or written to disk. A wrong code means nothing is deleted.
  - **Test delete role** (Retention) proves the setup end to end, unlock plus
    permission, with the same harmless probe, removing nothing.
  - The Retention panel shows the everyday user as append-only, plus a line
    saying deleting goes through the role. It warns if the everyday user can
    still delete on its own.
- **`deploy/iam/`**: `make-policies.sh` writes the policies for your account
  and buckets without calling AWS: the master's everyday user (with an explicit
  Deny on deletes), an agent user, the delete role, and its MFA-required trust
  policy. The README walks through creating them, keeping an existing user, MFA,
  agents and adding buckets.
- `awsapi.aws()` can run a single command as another identity (`creds=`), without
  touching the process environment.

## 1.18.0

- **A second machine (agent).** Another computer — e.g. a Raspberry Pi next to the
  master — can upload its own drives and receive downloads, with everything
  recorded in the master's one database and managed from the master's one web UI.
  Run it with `docker-compose.agent.yml` and `.env.agent.example`; see README
  "Second machine (agent)".
  - **Canary drives** plugged into the agent upload automatically, with the same
    rules as on the master (bucket from the canary, one upload per plug-in, no
    re-upload of a drive left plugged in across a restart).
  - **Upload from** (manual upload) and **Download to** (Downloads tab) pick the
    machine. The agent browses, plans and checks its own disk, then reads or
    writes it straight to or from S3. "Local file", Check local files and the
    leftover-file scan follow the chosen machine.
  - The Dashboard gets an **Agents** box (online/offline, activity, drives,
    download folder and free space, version). Sessions tables gain a Machine
    column, and agent log lines appear in the Logs tab prefixed with its name.
  - **Both machines upload at the same time.** Each file's S3 key is reserved just
    before upload, so two machines never write one object at once: the second
    waits, then skips if the content is identical. **Different content from a
    different machine under the same key is refused** (two drives with the same
    label), not allowed to replace the other machine's object. The same machine
    replacing its own changed file works as before.
  - **The agent has no database and stops without the master.** The file in
    flight finishes, its record waits, nothing new starts, and it resumes when
    the master is back. A master restart doesn't disturb an agent's session. An
    agent restart, or silence beyond `COLDVAULT_AGENT_FAIL_MINUTES` (10), fails
    its interrupted sessions; re-running skips verified files. `/health` reports
    agents and turns to `attention` while an offline agent owes work.
  - **Security.** Agents present a shared token (`COLDVAULT_AGENT_TOKEN`; none set =
    agents refused) and may change only their own sessions' records, through a
    fixed list of database calls. Retried calls are never applied twice. Bucket,
    prefix, storage class, canary name and checks are sent by the master, so the
    two can't drift. The README has an IAM policy for an agent user without
    delete rights.
- Fixes found along the way, which also apply with no agent at all:
  - A read error on a file no longer marks an already-verified index entry for
    the same key as failed. That object is still safely in S3.
  - Downloads: a skipped or failed file could have its status written onto
    another file's row when several downloaded in parallel.
  - Download sessions cut off by a restart are now marked failed instead of
    staying "running".
- `env-check.sh` compares an agent's `.env` with `.env.agent.example`.
- New settings on the master: `COLDVAULT_NAME`, `COLDVAULT_AGENT_TOKEN`,
  `COLDVAULT_AGENT_FAIL_MINUTES`. Existing databases are upgraded in place; all
  past sessions and downloads count as the master's own.

## 1.17.1

- **`env-check.sh`** — run it after `git pull` to see what's new in
  `.env.example`. It lists the settings your `.env` doesn't mention (grouped by
  section, with their default and explanation) and any setting in your `.env`
  that `.env.example` doesn't know, which usually means a typo or a rename.
  - Only setting names are read from `.env`; values (your keys) are never
    printed. This replaces the README's old `diff` tip, which printed both files
    in full, secrets included.
  - `--append` adds the new settings to the end of `.env` **commented out**. The
    configuration doesn't change (verified: ColdVault parses identically before
    and after). Commented-out settings count as seen, so the next check is
    clean.
  - Exit status 0/1/2 (clean / something listed / error), for use in scripts.
    Works with GNU and BSD tools: tested on Debian 12 (`mawk`) and macOS.
- README: `docker compose restart` does **not** apply `.env` changes. The
  container keeps the environment it was created with; use `docker compose up
  -d`. The old note said otherwise.
- `downloads/` and `backups/` are excluded from the Docker build context.

## 1.17.0

- **Local backup of ColdVault's own data.** A new **Settings** tab (it replaces the
  Notifications tab, which moves inside it) has a **Backup** section. The index
  — upload checksums, manifest results, sessions, restores, deletion records,
  settings — exists only in ColdVault's database. It's now snapshotted daily to
  `./backups/latest`, for rsync, borg or restic to pick up.
  - The snapshot uses SQLite's online backup API and passes `PRAGMA
    integrity_check`. Copying the live `coldvault.db` can catch a half-written
    file; this can't.
  - Alongside the `.db`: `files.csv` and `deletions.csv` (the index readable
    without SQLite), the logs, `config.json` (credentials excluded), and
    `backup.json` (row counts plus a sha256 for every file).
  - The snapshot is built aside and swapped into `latest/` only when complete,
    so `latest/` is never partial and a failed run leaves the previous one
    intact. The last N snapshots are kept in `history/` as hard links.
  - Daily at 03:00 with 7 kept by default. Both can be changed in the UI or
    via `COLDVAULT_BACKUP_AT` / `COLDVAULT_BACKUP_KEEP`. A missed slot runs on
    startup, and a failure is retried an hour later.
  - **`POST /api/backup`** returns only once the snapshot is finished (HTTP 500
    if it failed). `curl -fsS -X POST …/api/backup && restic backup …` can never
    back up a half-made snapshot.
  - `/health` gains a `backup` block and reports `attention` when the last
    backup failed or a scheduled one hasn't succeeded in two days.
  - Files are left uncompressed so borg/restic can deduplicate. `.env` is never
    copied: back it up yourself.
- Both compose files mount `./backups:/backups`. Recreate the container after
  pulling: `docker compose up -d --build`.

## 1.16.0

- **Retention covers every bucket, so it matches the Tree.** An expiry set or a
  deletion made from the Tree in a non-active bucket was invisible on the Retention
  tab, which only looked at the active bucket (or the Index tab's filter). It now
  has its own **Bucket** picker, defaulting to **all buckets**:
  - The **Expiry schedule** lists every bucket's scheduled objects with a Bucket
    column. Per-row *clear* and *Clear all shown* clear each row in its own bucket.
  - The **Deletion log** shows deletions from every bucket, including Tree deletes.
  - The permission panel shows one line per bucket. Each bucket is still probed
    once and remembered.
  - Prefix **Preview** and **Set/Clear expiry** run across the chosen buckets and
    report per bucket.
  - **Deleting is still one bucket at a time.** *Delete previewed* refuses a
    preview that matched several buckets, and *Delete all due* refuses when items
    are due in several, asking you to pick the bucket. The typed confirmation
    names the bucket. The API also refuses `bucket: "*"` on delete.
- The Retention tab no longer follows the Index tab's bucket filter.

## 1.15.0

- **Tree shows every bucket.** The Tree tab's top level is now every bucket in the
  index (plus the active one), each with its object count and size; click a bucket
  to open it. The active bucket opens on first visit, and whatever you open stays
  open across refreshes, per bucket.
  - **Restore** and **expiry** act across buckets in one go — the selection is sent
    to each bucket separately.
  - **Delete** stays one bucket at a time (its typed confirmation is the bucket
    name), and is disabled with a reason when the selection spans buckets or this
    IAM user can't delete from that bucket.
  - **Export** gets a bucket picker; each export is still one bucket's whole tree.
- The tree no longer follows the Index tab's bucket filter.

## 1.14.0

- **The canary picks the bucket.** A canary file can now name the bucket its drive
  belongs in — `{"name": "SSD01", "bucket": "my-archive-bucket"}`, or a `bucket:` line in
  a plain-text canary — so auto-upload can feed several buckets without touching the
  UI. A canary that names no bucket goes to the active bucket, as before; every
  existing canary format still parses the same way.
  - A named bucket that can't be used (typo, no access, not allowlisted) stops that
    drive's upload instead of falling back to the active bucket. The reason shows
    under *Active drives* on the dashboard and is logged once per plug-in.
  - `COLDVAULT_CANARY_BUCKETS` optionally limits which buckets a canary may name,
    since the canary lives on removable media.
  - Restart handling follows the canary's bucket: a still-mounted drive whose last
    upload to *its* bucket finished cleanly isn't re-uploaded.
- **Manual uploads name their bucket.** A new *Upload to bucket* picker (active
  bucket preselected, plus every known bucket and *other bucket…*) sits next to
  Start upload, and the confirmation states the destination. The server checks the
  bucket is reachable before queueing. Browsing into a folder with a canary
  preselects its bucket and label. Choosing here doesn't change the active bucket.
- Post-upload email audits now audit the bucket the session actually uploaded to,
  not whichever bucket happens to be active.

## 1.13.0

- **Local files not in the index** (Downloads tab) — scan the download folder for
  files no index row points at. They accumulate silently: clearing one bucket's
  index deletes its `downloads` rows, but the bytes stay on disk with nothing in
  the app mentioning them.
  - A downloaded file's path under the destination *is* its S3 key, so each orphan
    is looked up across **every** indexed bucket and shown as *in &lt;bucket&gt;*,
    *size differs*, or **not archived** — answering the only question that matters
    before deleting: is this still in the archive, or the last copy?
  - **Delete selected** removes the **local** copy only; S3 is never touched. A
    file that isn't archived anywhere is refused by the API unless explicitly
    forced, and the UI makes you type DELETE for those. Paths outside the download
    folder are always refused, and directories left empty are tidied up.
  - **Select still-archived** ticks only the files that are safely re-downloadable.
- **Clearing one bucket's index no longer leaves phantom download sessions.**
  `dl_sessions` has no bucket column, so a per-bucket clear removed the file rows
  but kept the sessions — the tab then showed "5/5 files · 51.6 GB (100%)" above an
  empty file list. Finished sessions with no remaining file rows are now dropped
  too (running/queued ones are left alone).

## 1.12.3

- **"Uploading now" stacks its files instead of running off the side.** The
  dashboard's key/value styling made *every* nested div a flex row, so the two
  concurrent uploads were laid out side by side and a full object key pushed the
  panel into a horizontal scroll. Each file is now its own line, and long keys and
  paths wrap inside the panel rather than widening it.

## 1.12.2

- **The delete-permission probe no longer runs on every render, and no longer
  looks like a failure.** It fired on each Retention/Tree load — including after
  every single-row expiry clear — filling the log with alarming `ERROR aws …
  DeleteObject … AccessDenied` lines. It never touched your data (the key is a
  random one under `.coldvault-permission-probe/` that has never existed, and S3's
  delete is idempotent), but it read like the app was trying to delete files.
  - Probed **once and remembered**; re-probed only via a new **Re-check** button.
  - Logged as one plain `retention` line stating the conclusion, not as an AWS
    error. `s3:GetBucketVersioning` being denied is likewise no longer an ERROR —
    not knowing simply means the preview can't warn about delete markers.
  - `COLDVAULT_DELETE_PROBE=false` disables it outright: ColdVault then never
    issues a delete call, and the banner reads "unknown".
  - As a side effect, on a versioned bucket the probe can no longer accumulate
    delete markers — at most one, ever, instead of one per page load.
- **Clear all shown** in the Expiry schedule unschedules the whole list in one
  call, instead of one click (and one log line) per object.

## 1.12.1

- **A scheduled expiry is now visible before it falls due.** The Retention table
  only ever listed objects whose date had already passed, so setting a date in the
  future looked as though it hadn't registered (the counter said "of 98 scheduled"
  but the table was empty). It's now an **Expiry schedule** with an *all
  scheduled* / *due now only* switch, defaulting to all, plus an **In** column
  counting down the days and a per-row **clear** to unschedule one object. The
  empty state explains what's scheduled and when the earliest one falls due
  instead of claiming there's nothing.

## 1.12.0

- **The tree no longer collapses when you tick something.** Ticking a folder used
  to re-render from the root, losing every open folder; the selection is now
  applied to the checkboxes in place. Open folders are also remembered across a
  genuine refresh (after a delete, say) and the page keeps its scroll position, so
  you stay where you were working.
- **Schedule expiry straight from the Tree tab** — pick a date and set or clear it
  for whole ticked folders (or individual files), alongside Restore and Delete.
  As in the Retention tab this only schedules: nothing is ever deleted
  automatically, and due items are listed there for a deliberate deletion.
- **Export the tree as Excel, PDF or CSV.** Every folder with its subtree's file
  count and size, the grand totals, and a header carrying the bucket name and the
  local date/time. The Excel workbook adds a second sheet listing every object
  (size, status, storage class, upload/verify times, SHA-256, manifest state,
  expiry) with filters on; the PDF is a paginated, indented report for printing or
  filing. Adds the `openpyxl` and `reportlab` dependencies — rebuild the image.

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
