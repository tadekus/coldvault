"""What to do about canary drives, wherever they're plugged in: this master's
own (reported by its watcher) and each agent's (reported at every check-in).
Master only.

A drive triggers exactly one upload session per plug-in; unplugging and
re-plugging triggers a new one (incremental — already-verified files are
skipped). The first report from a machine after it (or this master) started
is treated with care: a drive whose last session for its bucket completed
cleanly is left alone, so a restart doesn't re-upload a drive left plugged in
(the udev mount is read-only, so it can't have gained files meanwhile). A
drive with no session, or whose last one failed or was interrupted, uploads
(or resumes) normally.
"""

import threading

import buckets
import config
import db
from logs import log_event


def _where(location):
    return f" on {location}" if location else ""


class DriveTracker:
    def __init__(self, start_session):
        # start_session(location, mount, label, bucket) -> session id
        self.start_session = start_session
        self.active = {}          # location -> {mount: info}
        self._lock = threading.Lock()

    def restarted(self, location):
        """`location` restarted: its next report is a first look again."""
        with self._lock:
            self.active.pop(location, None)

    def drives(self, location):
        with self._lock:
            return dict(self.active.get(location, {}))

    def update(self, location, found):
        """`found` = {mount: {"label", "bucket"}} as scanned on `location`."""
        with self._lock:
            first = location not in self.active
            seen = self.active.setdefault(location, {})
            gone = [m for m in seen if m not in found]
            for m in gone:
                del seen[m]
            new = {m: i for m, i in found.items() if m not in seen}
            for m, i in new.items():   # placeholder so a slow decision isn't repeated
                seen[m] = {"label": i["label"], "bucket": i["bucket"], "session_id": None,
                           "detected_at": db.now(), "error": None}
        for m in gone:
            log_event("INFO", "watcher", f"drive unplugged / canary gone{_where(location)}: {m}")
        for mount, info in new.items():
            try:
                result = self._decide(location, mount, info, first)
            except Exception as e:
                log_event("ERROR", "watcher", f"canary handling failed for {mount}"
                          f"{_where(location)}: {e}")
                result = {"label": info["label"], "bucket": info["bucket"], "session_id": None,
                          "detected_at": db.now(), "error": str(e)}
            with self._lock:
                if mount in self.active.get(location, {}):
                    self.active[location][mount] = result

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

    def _decide(self, location, mount, info, first):
        where = _where(location)
        if first:
            # Only the local DB is read here, so don't spend a head-bucket call.
            bucket = info["bucket"] or config.BUCKET
            if db.latest_session_status(mount, bucket, location) == "done":
                log_event("INFO", "watcher",
                          f"drive already uploaded to s3://{bucket} (last session done), "
                          f"still mounted at {mount}{where} — not re-uploading; replug to re-scan")
                return {"label": info["label"], "bucket": bucket, "session_id": None,
                        "detected_at": db.now(), "error": None, "seeded": True}

        bucket, err = self._resolve_bucket(info)
        log_event("INFO", "watcher",
                  f"canary detected at {mount}{where} (label='{info['label']}'"
                  + (f", canary bucket='{info['bucket']}'" if info["bucket"]
                     else ", no bucket named — using the active one") + ")")
        sid = None
        if err:
            # Kept in `active` with the error, so this is logged once per
            # plug-in instead of every scan.
            log_event("ERROR", "watcher",
                      f"not uploading {mount}{where}: {err} — fix the canary "
                      f"(or COLDVAULT_CANARY_BUCKETS) and replug the drive")
        elif config.AUTO_UPLOAD:
            sid = self.start_session(location, mount, info["label"], bucket)
        else:
            log_event("WARNING", "watcher",
                      f"auto-upload disabled — start upload of {mount}{where} "
                      f"manually (target s3://{bucket})")
        return {"label": info["label"], "bucket": bucket or info["bucket"],
                "session_id": sid, "detected_at": db.now(), "error": err}
