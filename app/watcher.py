import json
import os
import threading
import time

import config
import db
from logs import log_event


def _read_canary(canary_path, mount):
    """Canary file contents decide the upload label (and optional extra prefix).
    Empty file -> volume directory name. Plain text -> first line is the label.
    JSON -> {"name": "...", "prefix": "..."}."""
    label = os.path.basename(mount.rstrip("/")) or "drive"
    prefix = ""
    try:
        with open(canary_path) as f:
            txt = f.read().strip()
        if txt:
            try:
                data = json.loads(txt)
                label = str(data.get("name", label)).strip() or label
                prefix = str(data.get("prefix", "")).strip("/")
            except json.JSONDecodeError:
                label = txt.splitlines()[0].strip() or label
    except OSError:
        pass
    return (f"{prefix}/{label}" if prefix else label)


class Watcher(threading.Thread):
    """Polls the watch roots for mounted drives containing the canary file.
    A drive triggers exactly one upload session per plug-in; unplugging and
    re-plugging triggers a new (incremental — already-verified files are
    skipped) session."""

    def __init__(self, uploader):
        super().__init__(daemon=True, name="watcher")
        self.uploader = uploader
        self.active = {}   # mount -> {label, session_id, detected_at}

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
                if db.latest_session_status(mount, config.BUCKET) == "done":
                    label = _read_canary(canary, mount)
                    self.active[mount] = {"label": label, "session_id": None,
                                          "detected_at": db.now(), "seeded": True}
                    log_event("INFO", "watcher",
                              f"drive already uploaded (last session done), still mounted "
                              f"at {mount} — not re-uploading; replug to re-scan")
        except Exception as e:
            log_event("ERROR", "watcher", f"seed error: {e}")

    def run(self):
        log_event("INFO", "watcher",
                  f"watching {', '.join(config.WATCH_DIRS)} for '{config.CANARY_NAME}' "
                  f"(auto_upload={'on' if config.AUTO_UPLOAD else 'off'}, "
                  f"every {config.WATCH_INTERVAL}s)")
        self._seed_active()
        while True:
            try:
                found = self._scan_mounts()
                for mount, canary in found.items():
                    if mount in self.active:
                        continue
                    label = _read_canary(canary, mount)
                    log_event("INFO", "watcher", f"canary detected at {mount} (label='{label}')")
                    sid = None
                    if config.AUTO_UPLOAD:
                        sid = self.uploader.enqueue(mount, label, "canary")
                    else:
                        log_event("WARNING", "watcher",
                                  f"auto-upload disabled — start upload of {mount} manually")
                    self.active[mount] = {"label": label, "session_id": sid,
                                          "detected_at": db.now()}
                for mount in [m for m in self.active if m not in found]:
                    log_event("INFO", "watcher", f"drive unplugged / canary gone: {mount}")
                    del self.active[mount]
            except Exception as e:
                log_event("ERROR", "watcher", f"scan error: {e}")
            time.sleep(config.WATCH_INTERVAL)
