"""Preview of a manual upload: which of the selected files would upload, and
which would be skipped or refused — decided by the same rules the uploader
applies, from the index alone. No AWS calls, and no file is read: where only a
checksum can tell (same size, different date; a possible duplicate), the file
is shown as "checked during upload". Master only.
"""

import os

import config
import db
from uploader import make_key

SHOW = 500   # files listed per group; the counts always cover everything

# id, will it upload ("yes" / "maybe" / "no"), what it means
GROUPS = [
    ("new", "yes", "New — uploads"),
    ("changed", "yes", "Changed since it was archived — uploads again and replaces "
                       "the archived copy (Deep Archive still bills the old copy's minimum)"),
    ("retry", "yes", "Not archived yet (an earlier upload failed or didn't finish) — uploads"),
    ("check", "maybe", "Archived with the same size but a different date — read and "
                       "compared during upload; skipped if the content is identical"),
    ("dup", "maybe", "New here, but an archived object in this bucket has the same size — "
                     "skipped if the content turns out identical (duplicate check)"),
    ("busy", "no", "Being uploaded right now by another session — waits, then skips "
                   "if that one archived the same content"),
    ("skip", "no", "Already archived and unchanged — skipped"),
    ("conflict", "no", "Different content is already archived under this name from another "
                       "machine — refused, nothing is overwritten (use another label)"),
]


def _group(r, size, mtime, location, dup_key, key):
    """Which group a file falls in, and a short note for it."""
    if r and r["status"] == "verified":
        other = (r["s_location"] or "") != (location or "") and r["session_id"] is not None
        where = r["s_location"] or config.NAME
        if r["size"] == size and abs((r["mtime"] or 0) - mtime) < 2:
            return "skip", None
        hashed = config.DEDUPE or size <= config.MULTIPART_THRESHOLD
        if r["size"] == size and hashed:
            return "check", f"archived from {where}" if other else None
        if other:
            return "conflict", f"archived from {where}"
        return "changed", f"archived copy is {r['size']:,} bytes" if r["size"] != size else None
    if r and r["status"] == "uploading" and r["s_status"] in db.ACTIVE:
        return "busy", f"session #{r['session_id']}"
    if r and r["status"] == "remote":
        return "changed", "in the bucket, but never uploaded and verified by ColdVault"
    if r:
        return "retry", r.get("error") or r["status"]
    if dup_key is not None and dup_key != key:
        return "dup", f"same size as {dup_key}"
    return "new", None


def classify(bucket, label, location, plan):
    """`plan` is localfs.plan_upload()'s answer from the machine with the files."""
    label = label or os.path.basename(plan["source"].rstrip("/")) or "upload"
    files = plan["files"]
    keys = [make_key(label, rel) for rel, _, _ in files]
    rows = db.preview_rows(bucket, keys)
    sizes = db.verified_sizes(bucket) if config.DEDUPE else {}
    out = {gid: {"id": gid, "uploads": up, "title": title, "count": 0, "bytes": 0, "files": []}
           for gid, up, title in GROUPS}
    for (rel, size, mtime), key in zip(files, keys):
        gid, note = _group(rows.get(key), size, mtime, location, sizes.get(size), key)
        g = out[gid]
        g["count"] += 1
        g["bytes"] += size
        if len(g["files"]) < SHOW:
            g["files"].append({"rel": rel, "key": key, "size": size, "note": note})

    def total(*ups):
        gs = [g for g in out.values() if g["uploads"] in ups]
        return {"count": sum(g["count"] for g in gs), "bytes": sum(g["bytes"] for g in gs)}

    return {
        "bucket": bucket, "label": label, "location": location or config.NAME,
        "source": plan["source"], "key_prefix": make_key(label, ""),
        "selected": {"count": len(files), "bytes": sum(f[1] for f in files)},
        "uploads": total("yes"), "maybe": total("maybe"), "skipped": total("no"),
        "groups": [dict(g, more=g["count"] - len(g["files"]))
                   for g in out.values() if g["count"]],
        "excluded": plan["excluded"], "excluded_count": plan["excluded_count"],
    }
