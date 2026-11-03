"""Local backup of ColdVault's own state, for rsync / borg / restic to pick up.

The index is the one thing ColdVault can't rebuild from S3: checksums verified
at upload, offload-manifest results, sessions, restore history, deletion
tombstones and settings all live only in coldvault.db. Copying that file while
the app runs can capture a half-written page, so instead ColdVault writes a
consistent snapshot to a folder of its own and your backup tool takes it from
there.

Layout of COLDVAULT_BACKUP_DIR:

  latest/               the newest complete snapshot — point your tool here
    coldvault.db        SQLite online-backup snapshot, integrity-checked
    files.csv           the index as plain text (bucket, key, size, sha256, …)
    deletions.csv       every deletion tombstone
    coldvault.log*      the log files
    config.json         the effective settings, secrets left out
    backup.json         manifest: when, version, trigger, row counts, sha256 per file
  history/<stamp>/      the newest BACKUP_KEEP snapshots (hard links, so the
                        newest costs no extra space)

A snapshot is built in a hidden .incoming-* folder and only renamed to
latest/ once it is complete and checked, so latest/ is always whole. Files
are written uncompressed: borg and restic compress and deduplicate
themselves, and a gzip would defeat their deduplication between runs.
"""

import csv
import glob
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta

import config
import db
import version
from logs import log_event

SETTINGS_KEY = "backup"        # {"at": "HH:MM" | "", "keep": n} saved from the UI
LAST_KEY = "backup_last"       # result of the most recent attempt
RETRY_AFTER = timedelta(hours=1)   # after a failed scheduled run
_SECRET_WORDS = ("KEY", "SECRET", "TOKEN", "PASSWORD")

_run_lock = threading.Lock()


class BackupBusy(RuntimeError):
    pass


# ---- settings ----

def parse_at(at):
    """'03:00' -> (3, 0); '' or 'off' -> None (on demand only). ValueError on
    anything else."""
    at = (at or "").strip().lower()
    if at in ("", "off", "none", "false", "0"):
        return None
    try:
        h, m = (int(x) for x in at.split(":"))
    except ValueError:
        raise ValueError(f"backup time must be HH:MM (or empty to turn it off), got '{at}'")
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"backup time must be HH:MM, got '{at}'")
    return h, m


def settings():
    """Effective schedule: .env defaults, overridden by what was saved in the UI."""
    s = {"at": config.BACKUP_AT, "keep": config.BACKUP_KEEP}
    try:
        s.update(json.loads(db.get_setting(SETTINGS_KEY) or "{}"))
    except ValueError:
        pass
    return s


def save_settings(at, keep):
    hm = parse_at(at)
    try:
        keep = int(keep)
    except (TypeError, ValueError):
        raise ValueError("keep must be a whole number")
    if not 0 <= keep <= 365:
        raise ValueError("keep must be between 0 and 365")
    value = {"at": f"{hm[0]:02d}:{hm[1]:02d}" if hm else "", "keep": keep}
    db.set_setting(SETTINGS_KEY, json.dumps(value))
    log_event("INFO", "backup",
              f"backup schedule set: {'daily at ' + value['at'] if hm else 'on demand only'}, "
              f"keeping {keep} snapshot(s) in history (saved, overrides .env)")
    return value


def last():
    try:
        return json.loads(db.get_setting(LAST_KEY) or "null")
    except ValueError:
        return None


# ---- schedule ----

def _ts(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def next_run(now=None):
    """When the scheduler will next run a backup (a datetime), or None if
    backups are on demand only. Today's slot counts as done once any backup —
    scheduled or manual — has succeeded since it; a failed one is retried an
    hour later rather than every few seconds."""
    try:
        hm = parse_at(settings()["at"])
    except ValueError:
        return None
    if not hm:
        return None
    now = now or datetime.now()
    slot = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    if now < slot:
        return slot
    lr = last()
    if lr and (lr.get("finished_at") or "") >= _ts(slot):
        if lr.get("ok"):
            return slot + timedelta(days=1)
        retry = datetime.strptime(lr["finished_at"], "%Y-%m-%d %H:%M:%S") + RETRY_AFTER
        return max(retry, now)
    return now   # missed today's slot (e.g. the container was down) — catch up


def _scheduler():
    time.sleep(60)   # let startup settle first
    try:
        parse_at(settings()["at"])
    except ValueError as e:
        log_event("ERROR", "backup", f"scheduled backups disabled: {e}")
    while True:
        try:
            nr = next_run()
            if nr and nr <= datetime.now() and not _run_lock.locked():
                run("scheduled")
        except BackupBusy:
            pass
        except Exception as e:
            log_event("ERROR", "backup", f"backup scheduler error: {e}")
        time.sleep(30)


def start():
    threading.Thread(target=_scheduler, daemon=True, name="backup").start()


# ---- the snapshot ----

def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(blk)
    return h.hexdigest()


def _write_csv(snap, path, sql):
    """CSV straight from the snapshot, so it matches the .db beside it exactly
    and never touches the live database."""
    con = sqlite3.connect(snap)
    try:
        cur = con.execute(sql)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([d[0] for d in cur.description])
            for row in cur:
                w.writerow(row)
    finally:
        con.close()


def _inspect(snap):
    """(integrity_check result, {table: row count}) of a snapshot file."""
    con = sqlite3.connect(snap)
    try:
        check = con.execute("PRAGMA integrity_check").fetchone()[0]
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name")]
        counts = {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
        return check, counts
    finally:
        con.close()


def _config_dump():
    """The settings that shaped this database — enough to rebuild the container
    around it. Anything that looks like a credential is left out; back up your
    .env separately (it's on the host)."""
    env = {k: v for k, v in sorted(os.environ.items())
           if (k.startswith("COLDVAULT_") or k in ("TZ", "AWS_DEFAULT_REGION"))
           and not any(w in k for w in _SECRET_WORDS)}
    return {"version": version.VERSION, "active_bucket": config.BUCKET,
            "storage_class": config.STORAGE_CLASS, "prefix": config.PREFIX,
            "environment": env}


def _fsync_tree(path):
    for name in os.listdir(path):
        with open(os.path.join(path, name), "rb") as f:
            os.fsync(f.fileno())
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish(root, work, stamp, keep):
    """Swap the finished snapshot in as latest/, then record it in history/."""
    latest = os.path.join(root, "latest")
    prev = None
    if os.path.isdir(latest):
        prev = os.path.join(root, f".previous-{stamp}")
        os.rename(latest, prev)
    os.rename(work, latest)
    if prev:
        shutil.rmtree(prev, ignore_errors=True)

    hist_root = os.path.join(root, "history")
    if keep > 0:
        hist = os.path.join(hist_root, stamp)
        os.makedirs(hist, exist_ok=True)
        for name in os.listdir(latest):
            src, dst = os.path.join(latest, name), os.path.join(hist, name)
            try:
                os.link(src, dst)       # same bytes, no extra space
            except OSError:
                shutil.copy2(src, dst)  # filesystem without hard links
    if os.path.isdir(hist_root):
        snaps = sorted(d for d in os.listdir(hist_root)
                       if os.path.isdir(os.path.join(hist_root, d)))
        for old in snaps[:max(0, len(snaps) - keep)]:
            shutil.rmtree(os.path.join(hist_root, old), ignore_errors=True)


def _clear_leftovers(root):
    """Half-built snapshots from a run that crashed or was killed."""
    for d in glob.glob(os.path.join(root, ".incoming-*")) + \
             glob.glob(os.path.join(root, ".previous-*")):
        shutil.rmtree(d, ignore_errors=True)


def run(trigger="manual"):
    """Take a snapshot now. Returns the result dict (also saved as the last
    result); raises BackupBusy if one is already running."""
    if not _run_lock.acquire(blocking=False):
        raise BackupBusy("a backup is already running")
    t0 = time.monotonic()
    started = db.now()
    root = config.BACKUP_DIR
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    work = os.path.join(root, f".incoming-{stamp}")
    keep = int(settings().get("keep", config.BACKUP_KEEP))
    result = {"ok": False, "trigger": trigger, "started_at": started, "dir": root}
    try:
        os.makedirs(root, exist_ok=True)
        _clear_leftovers(root)
        os.makedirs(work)

        snap = os.path.join(work, "coldvault.db")
        db.snapshot(snap)
        check, counts = _inspect(snap)
        if check != "ok":
            raise RuntimeError(f"snapshot failed SQLite integrity_check: {check[:300]}")

        _write_csv(snap, os.path.join(work, "files.csv"),
                   "SELECT bucket, key, size, sha256, checksum_s3, etag, storage_class, status, "
                   "uploaded_at, verified_at, manifest_state, manifest_algo, manifest_hash, "
                   "expires_at FROM files ORDER BY bucket, key")
        _write_csv(snap, os.path.join(work, "deletions.csv"),
                   "SELECT * FROM deletions ORDER BY id")
        for lp in sorted(glob.glob(config.LOG_PATH + "*")):
            shutil.copy2(lp, work)
        with open(os.path.join(work, "config.json"), "w") as f:
            json.dump(_config_dump(), f, indent=2)

        files = {name: {"bytes": os.path.getsize(os.path.join(work, name)),
                        "sha256": _sha256(os.path.join(work, name))}
                 for name in sorted(os.listdir(work))}
        manifest = {
            "created_at": db.now(), "coldvault_version": version.VERSION,
            "trigger": trigger, "integrity_check": check, "rows": counts,
            "files": files,
            "restore": "Stop ColdVault, copy coldvault.db into the data folder "
                       "(replacing the old one), start ColdVault.",
        }
        with open(os.path.join(work, "backup.json"), "w") as f:
            json.dump(manifest, f, indent=2)
        _fsync_tree(work)
        _publish(root, work, stamp, keep)

        total = sum(v["bytes"] for v in files.values())
        result.update(ok=True, snapshot=stamp, bytes=total, files=len(files) + 1,
                      rows=counts, path=os.path.join(root, "latest"))
        log_event("INFO", "backup",
                  f"backup OK ({trigger}): {root}/latest — {total:,} bytes, "
                  f"{counts.get('files', 0):,} indexed objects, integrity ok"
                  + (f", {keep} kept in history" if keep else ""))
    except Exception as e:
        shutil.rmtree(work, ignore_errors=True)
        result["error"] = str(e)[:500]
        log_event("ERROR", "backup", f"backup FAILED ({trigger}) to {root}: {e}")
    finally:
        result["finished_at"] = db.now()
        result["seconds"] = round(time.monotonic() - t0, 2)
        try:
            db.set_setting(LAST_KEY, json.dumps(result))
        finally:
            _run_lock.release()
    return result


# ---- status ----

def status():
    root = config.BACKUP_DIR
    exists = os.path.isdir(root)
    st = settings()
    nr = next_run()
    info = {
        "dir": root, "exists": exists,
        "writable": exists and os.access(root, os.W_OK),
        # In Docker, a folder that isn't a mount point lives inside the
        # container and disappears with it — worth shouting about.
        "mounted": os.path.ismount(root),
        "in_docker": os.path.exists("/.dockerenv"),
        "at": st.get("at") or "", "keep": st.get("keep", config.BACKUP_KEEP),
        "env": {"at": config.BACKUP_AT, "keep": config.BACKUP_KEEP},
        "next_run": _ts(nr) if nr else None,
        "running": _run_lock.locked(),
        "last": last(),
        "latest": None, "history": [],
    }
    if exists:
        try:
            info["free_bytes"] = shutil.disk_usage(root).free
        except OSError:
            pass
        try:
            with open(os.path.join(root, "latest", "backup.json")) as f:
                m = json.load(f)
            info["latest"] = {"created_at": m.get("created_at"),
                              "trigger": m.get("trigger"),
                              "rows": m.get("rows", {}),
                              "files": {k: v["bytes"] for k, v in m.get("files", {}).items()}}
        except (OSError, ValueError):
            pass
        hist = os.path.join(root, "history")
        if os.path.isdir(hist):
            info["history"] = sorted((d for d in os.listdir(hist)
                                      if os.path.isdir(os.path.join(hist, d))), reverse=True)
    return info


def health():
    """(summary, needs_attention) for /health: a failed last backup, or a
    schedule that hasn't produced a good backup in two days."""
    lr = last()
    try:
        scheduled = parse_at(settings()["at"]) is not None
    except ValueError:
        scheduled = False
    summary = {"scheduled": scheduled, "last_ok": bool(lr and lr.get("ok")),
               "last_at": lr and lr.get("finished_at"),
               "last_error": lr and lr.get("error")}
    stale = False
    if scheduled and lr and lr.get("ok"):
        stale = lr["finished_at"] < _ts(datetime.now() - timedelta(days=2))
    summary["stale"] = stale
    return summary, bool(lr and not lr.get("ok")) or stale
