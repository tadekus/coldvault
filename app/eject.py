"""Drives ready to be unmounted after a clean canary upload.

The container can't unmount a host filesystem, so each machine — the master
for its own drives, an agent for its own — only publishes the drive on
/api/eject/pending, and the host-side helper (deploy/usb-automount/
coldvault-eject.sh) unmounts it and reports back on /api/eject/done.
"""

import config
from logs import log_event
from store import db

pending = {}   # mount -> {label, session_id, files, bytes, since}


def after_session(s, busy):
    """Flag a finished canary session's drive for unmount — only when the
    session was completely clean and nothing else is queued (`busy`)."""
    if not config.EJECT_AFTER_UPLOAD or not s or s.get("trigger") != "canary":
        return
    sid = s["id"]
    try:
        busy = busy or s["source"] in pending
        if s["status"] == "done" and not s["failed_files"] and not busy:
            pending[s["source"]] = {
                "label": s["label"], "session_id": sid,
                "files": s["done_files"], "bytes": s["done_bytes"], "since": db.now()}
            log_event("INFO", "eject",
                      f"{s['source']} ready to unmount — session #{sid} completed "
                      f"with {s['done_files']} file(s), 0 failed")
        elif s["failed_files"] or s["status"] != "done":
            log_event("WARNING", "eject",
                      f"not flagging {s['source']} for unmount — session #{sid} "
                      f"status={s['status']}, {s['failed_files']} failed")
    except Exception as e:
        log_event("ERROR", "eject", f"eject flagging failed for #{sid}: {e}")


def listing():
    return {"enabled": config.EJECT_AFTER_UPLOAD,
            "pending": [dict(mount=m, **v) for m, v in pending.items()]}


def done(mount, ok, detail):
    """The host helper reports the outcome of an unmount."""
    info = pending.pop(mount, None)
    if ok:
        log_event("INFO", "eject", f"unmounted {mount}"
                  + (f" (label '{info['label']}')" if info else "") + " — safe to remove")
    else:
        log_event("ERROR", "eject", f"host failed to unmount {mount}: {detail or 'unknown'}")
        if info:
            pending[mount] = info   # keep it pending so the helper retries
    return len(pending)
