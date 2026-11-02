import json
import os
import re
import threading
import time

import buckets
import config
import db
from logs import log_event

# Plain-text canary directives, recognised on any line: "bucket: name",
# "prefix = raw", "name: movie-drive-01".
_DIRECTIVE = re.compile(r"^(bucket|prefix|name)\s*[:=]\s*(.+)$", re.IGNORECASE)


def read_canary(canary_path, mount):
    """Canary file contents decide the upload label, an optional extra key
    prefix, and optionally which bucket the drive belongs in.

    Empty file -> label is the volume directory name, bucket is the active one.
    JSON       -> {"name": "...", "prefix": "...", "bucket": "..."}
    Plain text -> the first bare line is the label; 'bucket:', 'prefix:' and
                  'name:' lines are read as directives wherever they appear,
                  and '#' lines are comments.

    Returns {"label": ..., "bucket": None|str}; bucket None means "whichever
    bucket is active". The name is returned unvalidated — buckets.check()
    decides whether it may actually be used."""
    label = os.path.basename(mount.rstrip("/")) or "drive"
    prefix = bucket = ""
    try:
        with open(canary_path) as f:
            txt = f.read().strip()
    except OSError:
        txt = ""

    data = None
    if txt.startswith("{"):
        try:
            data = json.loads(txt)
        except json.JSONDecodeError:
            data = None
    if isinstance(data, dict):
        label = str(data.get("name", label)).strip() or label
        prefix = str(data.get("prefix", "")).strip("/")
        bucket = str(data.get("bucket", "")).strip()
    elif txt:
        have_label = False
        for line in txt.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = _DIRECTIVE.match(line)
            if not m:
                if not have_label:
                    label, have_label = line, True
                continue
            key, val = m.group(1).lower(), m.group(2).strip()
            if key == "bucket":
                bucket = val
            elif key == "prefix":
                prefix = val.strip("/")
            elif val:
                label, have_label = val, True

    return {"label": f"{prefix}/{label}" if prefix else label,
            "bucket": bucket or None}


class Watcher(threading.Thread):
    """Polls the watch roots for mounted drives containing the canary file.
    A drive triggers exactly one upload session per plug-in; unplugging and
    re-plugging triggers a new (incremental — already-verified files are
    skipped) session."""

    def __init__(self, uploader):
        super().__init__(daemon=True, name="watcher")
        self.uploader = uploader
        self.active = {}   # mount -> {label, bucket, session_id, detected_at, error}

    def _scan_mounts(self):
        """Return {mount_path: canary_path} for every canary found.
        Checks watch roots plus two levels below (Debian: /media/<user>/<label>)."""
        found = {}
        for root in config.WATCH_DIRS:
            candidates = [root]
            try:
                for d1 in os.scandir(root):
                    if d1.is_dir(follow_symlinks=False):
                        candidates.append(d1.path)
                        try:
                            candidates += [d2.path for d2 in os.scandir(d1.path)
                                           if d2.is_dir(follow_symlinks=False)]
                        except OSError:
                            pass
            except OSError:
                continue
            for mount in candidates:
                canary = os.path.join(mount, config.CANARY_NAME)
                if os.path.isfile(canary):
                    found[mount] = canary
        return found

    @staticmethod
    def _resolve_bucket(info):
        """Returns (bucket, error). A canary that names a bucket we can't use
        stops the upload rather than quietly sending the drive to the active
        bucket — the wrong bucket is worse than no upload."""
        if not info["bucket"]:
            return config.BUCKET, None if config.BUCKET else "no bucket configured"
        try:
            return buckets.check(info["bucket"], source="canary"), None
        except buckets.BucketError as e:
            return None, str(e)

    def _seed_active(self):
        """On startup, treat drives that are ALREADY mounted and whose most
        recent session for this bucket completed cleanly ('done') as already
        handled, so a container restart doesn't re-upload a drive left plugged
        in. A drive with no session, or whose last session failed / was
        interrupted, is left unseeded so it uploads (or resumes) normally.
        (Our udev mount is read-only, so a still-mounted drive can't have gained
        new files since it was uploaded.)"""
        try:
            for mount, canary in self._scan_mounts().items():
                info = read_canary(canary, mount)
                # The canary's own bucket decides which session history counts;
                # this only reads the local DB, so don't spend a head-bucket
                # call validating it at startup.
                bucket = info["bucket"] or config.BUCKET
                if db.latest_session_status(mount, bucket) == "done":
                    self.active[mount] = {"label": info["label"], "bucket": bucket,
                                          "session_id": None, "detected_at": db.now(),
                                          "error": None, "seeded": True}
                    log_event("INFO", "watcher",
                              f"drive already uploaded to s3://{bucket} (last session done), "
                              f"still mounted at {mount} — not re-uploading; replug to re-scan")
        except Exception as e:
            log_event("ERROR", "watcher", f"seed error: {e}")

    def run(self):
        log_event("INFO", "watcher",
                  f"watching {', '.join(config.WATCH_DIRS)} for '{config.CANARY_NAME}' "
                  f"(auto_upload={'on' if config.AUTO_UPLOAD else 'off'}, "
                  f"every {config.WATCH_INTERVAL}s, canary buckets: "
                  f"{', '.join(config.CANARY_BUCKETS) or 'any accessible'})")
        self._seed_active()
        while True:
            try:
                found = self._scan_mounts()
                for mount, canary in found.items():
                    if mount in self.active:
                        continue
                    info = read_canary(canary, mount)
                    bucket, err = self._resolve_bucket(info)
                    log_event("INFO", "watcher",
                              f"canary detected at {mount} (label='{info['label']}'"
                              + (f", canary bucket='{info['bucket']}'" if info["bucket"]
                                 else ", no bucket named — using the active one") + ")")
                    sid = None
                    if err:
                        # Stays in self.active with the error, so this is logged
                        # once per plug-in instead of every scan.
                        log_event("ERROR", "watcher",
                                  f"not uploading {mount}: {err} — fix the canary "
                                  f"(or COLDVAULT_CANARY_BUCKETS) and replug the drive")
                    elif config.AUTO_UPLOAD:
                        sid = self.uploader.enqueue(mount, info["label"], "canary",
                                                    bucket=bucket)
                    else:
                        log_event("WARNING", "watcher",
                                  f"auto-upload disabled — start upload of {mount} "
                                  f"manually (target s3://{bucket})")
                    self.active[mount] = {"label": info["label"],
                                          "bucket": bucket or info["bucket"],
                                          "session_id": sid, "detected_at": db.now(),
                                          "error": err}
                for mount in [m for m in self.active if m not in found]:
                    log_event("INFO", "watcher", f"drive unplugged / canary gone: {mount}")
                    del self.active[mount]
            except Exception as e:
                log_event("ERROR", "watcher", f"scan error: {e}")
            time.sleep(config.WATCH_INTERVAL)
