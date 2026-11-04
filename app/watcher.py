import json
import os
import re
import threading
import time

import config
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


def scan_mounts():
    """{mount: {"label", "bucket"}} for every mounted drive with a canary in
    its root. Checks the watch roots plus two levels below (Debian:
    /media/<user>/<label>). Reading only — what to do about a drive is the
    master's decision (drives.py), wherever the drive is plugged in."""
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
                found[mount] = read_canary(canary, mount)
    return found


class Watcher(threading.Thread):
    """Polls the watch roots for canary drives every COLDVAULT_WATCH_INTERVAL
    seconds and hands what it sees to `report(found)`: on the master that's the
    drive tracker, on an agent it's the next check-in with the master."""

    def __init__(self, report):
        super().__init__(daemon=True, name="watcher")
        self.report = report
        self.last = None   # None until the first scan: "don't know yet", not "no drives"

    def run(self):
        log_event("INFO", "watcher",
                  f"watching {', '.join(config.WATCH_DIRS)} for '{config.CANARY_NAME}' "
                  f"every {config.WATCH_INTERVAL}s")
        while True:
            try:
                self.last = scan_mounts()
                self.report(self.last)
            except Exception as e:
                log_event("ERROR", "watcher", f"scan error: {e}")
            time.sleep(config.WATCH_INTERVAL)
