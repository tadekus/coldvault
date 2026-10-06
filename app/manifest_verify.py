"""Verify an offload manifest against what's already archived ("verify later").

Two levels:
  1. Index cross-check (instant): every manifest file is looked up in the index
     by filename (+ optional key scope), sizes compared, and the manifest hash is
     attached to the matched object so a later restore+download re-verifies it
     byte-for-byte (v1.6.0). No file bytes are read.
  2. Deep verify (opt-in, background): if the original source file is still on a
     mounted drive (the path recorded at upload), re-read it, computing SHA-256 and
     the manifest hash in one pass. If the SHA-256 equals the archived object's,
     the local bytes ARE the archived bytes, so the manifest comparison is a true
     verification of the archive — without restoring anything from Deep Archive.
"""
import hashlib
import itertools
import os
import threading
import time

import db
import manifest
from logs import log_event

READ_BLOCK = 8 * 1024 * 1024
_jobs = {}
_job_ids = itertools.count(1)
_lock = threading.Lock()


def _suffix_score(key, paths):
    """How many trailing path components the index key shares with any of the
    manifest's recorded paths — used to pick the right file when a name (e.g.
    '4-10T01.wav') exists in several shoots."""
    kparts = [p.lower() for p in key.split("/") if p]
    best = 0
    for p in paths:
        pparts = [x.lower() for x in p.split("/") if x and x not in (".", "..")]
        n = 0
        while n < len(kparts) and n < len(pparts) and kparts[-1 - n] == pparts[-1 - n]:
            n += 1
        best = max(best, n)
    return best


def _candidates(rec, bucket, scope):
    names = [rec["name"]] + sorted(a for a in rec["aliases"] if "." in a and a != rec["name"].lower())
    for n in names:
        rows = db.files_by_basename(bucket, n, scope)
        if rows:
            return rows
    # Manifest lists a bare clip name with no extension (Silverstack exports
    # without a Resources column): match the archived file by its stem.
    if not os.path.splitext(rec["name"])[1]:
        return db.files_by_stem(bucket, rec["name"], scope)
    return []


def verify(records, bucket=None, scope=None):
    """Cross-check manifest records against the index. Attaches manifest hashes
    to matched objects. Returns a report dict (plus '_deep' rows for deep verify)."""
    rep = {"total": len(records), "verified": 0, "mismatch": 0, "present": 0,
           "size_mismatch": 0, "missing": 0, "ambiguous": 0,
           "missing_list": [], "size_mismatch_list": [], "mismatch_list": [],
           "ambiguous_list": [], "_deep": []}
    for rec in records:
        rows = _candidates(rec, bucket, scope)
        if not rows:
            rep["missing"] += 1
            rep["missing_list"].append(rec["name"])
            continue
        sized = [r for r in rows if rec["size"] is None or r["size"] is None
                 or int(r["size"]) == int(rec["size"])]
        if not sized:
            rep["size_mismatch"] += 1
            rep["size_mismatch_list"].append(
                {"name": rec["name"], "manifest": rec["size"], "archived": rows[0]["size"],
                 "key": rows[0]["key"]})
            continue
        scores = {r["id"]: _suffix_score(r["key"], rec["paths"]) for r in sized}
        top = max(scores.values())
        picked = [r for r in sized if scores[r["id"]] == top]
        if len(picked) > 1:
            rep["ambiguous"] += 1
            rep["ambiguous_list"].append({"name": rec["name"], "keys": [r["key"] for r in picked]})

        for r in picked:
            prev = r.get("manifest_state")
            if prev in ("ok", "mismatch") and r.get("manifest_hash"):
                same = r["manifest_hash"].lower() == rec["hash"]
                if prev == "ok" and same:
                    rep["verified"] += 1
                    continue
                rep["mismatch"] += 1
                rep["mismatch_list"].append({"name": rec["name"], "key": r["key"]})
                continue
            # Attach the hash so download re-verify (and deep verify) can check bytes.
            db.set_manifest(r["id"], "listed", rec["algo"], rec["hash"])
            rep["present"] += 1
            rep["_deep"].append({"id": r["id"], "algo": rec["algo"], "hash": rec["hash"]})

    for k in ("missing_list", "size_mismatch_list", "mismatch_list", "ambiguous_list"):
        rep[k] = rep[k][:200]
    lvl = "ERROR" if (rep["mismatch"] or rep["size_mismatch"]) else (
        "WARNING" if rep["missing"] else "INFO")
    log_event(lvl, "manifest",
              f"manifest verify: {rep['total']} files — {rep['verified']} byte-verified, "
              f"{rep['present']} present (name+size ok, hash attached), "
              f"{rep['size_mismatch']} size mismatch, {rep['mismatch']} hash mismatch, "
              f"{rep['missing']} not archived"
              + (f" (scope '{scope}')" if scope else ""))
    for m in rep["size_mismatch_list"]:
        log_event("ERROR", "manifest",
                  f"size mismatch vs manifest: {m['key']} manifest={m['manifest']} archived={m['archived']}")
    return rep


# ------------------------------------------------------------- deep verify

def _hash_pair(path, algo):
    sha = hashlib.sha256()
    extra = manifest.new_hasher(algo)
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(READ_BLOCK), b""):
            sha.update(blk)
            if extra is not None:
                extra.update(blk)
    return sha.hexdigest(), (extra.hexdigest() if extra is not None else None)


def start_deep(items, label):
    jid = next(_job_ids)
    job = {"id": jid, "label": label, "status": "running", "total": len(items),
           "done": 0, "ok": 0, "mismatch": 0, "changed": 0, "no_local": 0,
           "unsupported": 0, "bytes_done": 0, "started_at": db.now(),
           "finished_at": None, "mismatch_list": []}
    with _lock:
        _jobs[jid] = job
    threading.Thread(target=_run_deep, args=(job, items), daemon=True,
                     name=f"manifest-deep-{jid}").start()
    log_event("INFO", "manifest", f"deep verify #{jid} started: {len(items)} file(s) ({label})")
    return jid


def _run_deep(job, items):
    for it in items:
        try:
            row = db.get_file_by_id(it["id"])
            path = (row or {}).get("local_path")
            if not row or not path or not os.path.isfile(path) \
                    or os.path.getsize(path) != int(row["size"] or -1):
                job["no_local"] += 1
                continue
            sha, mh = _hash_pair(path, it["algo"])
            job["bytes_done"] += int(row["size"] or 0)
            if mh is None:
                job["unsupported"] += 1
                db.set_manifest(row["id"], "algo_unsupported", it["algo"], it["hash"])
            elif sha != (row["sha256"] or ""):
                # local file is no longer the archived bytes — can't conclude
                job["changed"] += 1
            elif manifest.normalize_hash(it["algo"], mh) == it["hash"]:
                job["ok"] += 1
                db.set_manifest(row["id"], "ok", it["algo"], it["hash"])
            else:
                job["mismatch"] += 1
                db.set_manifest(row["id"], "mismatch", it["algo"], it["hash"])
                job["mismatch_list"].append(row["key"])
                log_event("ERROR", "manifest",
                          f"DEEP VERIFY MISMATCH: {row['key']} — archived bytes != offload {it['algo']}")
        except Exception as e:
            log_event("ERROR", "manifest", f"deep verify error on id {it.get('id')}: {e}")
        finally:
            job["done"] += 1
    job["status"] = "done"
    job["finished_at"] = db.now()
    job["mismatch_list"] = job["mismatch_list"][:200]
    lvl = "ERROR" if job["mismatch"] else "INFO"
    log_event(lvl, "manifest",
              f"deep verify #{job['id']} finished: {job['ok']} ok, {job['mismatch']} mismatch, "
              f"{job['changed']} local changed, {job['no_local']} source not available, "
              f"{job['unsupported']} unsupported")


def jobs():
    with _lock:
        return sorted(({k: v for k, v in j.items()} for j in _jobs.values()),
                      key=lambda j: -j["id"])
