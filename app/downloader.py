"""Download restored objects back to a local folder, preserving the S3 key
structure as directories, and verify each file against the SHA-256 recorded
in the index when it was uploaded."""
import hashlib
import json
import os
import queue
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import awsapi
import config
import manifest
from awsapi import AwsError
from logs import log_event
from store import db

READ_BLOCK = 8 * 1024 * 1024


def fmt_speed(bps):
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bps < 1000 or unit == "GB/s":   # decimal (SI), as transfer tools report
            return f"{bps:.1f} {unit}"
        bps /= 1000


def restore_expired(expiry):
    """True if a restore's expiry is in the past — the object is no longer
    available to download. Accepts our local format ('2027-07-31 00:00:00',
    stored since v1.9.0) and the raw S3 GMT form kept on older rows."""
    if not expiry:
        return False
    from datetime import datetime, timezone
    s = str(expiry).strip()
    try:  # local naive (current format)
        return datetime.strptime(s, db.TIME_FMT) < datetime.now()
    except ValueError:
        pass
    try:  # legacy rows: RFC 1123 GMT from S3
        from email.utils import parsedate_to_datetime
        exp = parsedate_to_datetime(s)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp < datetime.now(timezone.utc)
    except Exception:
        return False


def verify_local_downloads():
    """Check that files recorded as downloaded still exist on the local
    filesystem; mark any that were deleted so the UI reflects reality.
    Returns (checked, deleted)."""
    checked = deleted = 0
    for r in db.downloaded_rows_to_check():
        checked += 1
        p = r["local_path"]
        if not p or not os.path.exists(p):
            db.update_download(r["id"], status="deleted",
                               error="file no longer on local filesystem")
            deleted += 1
            log_event("WARNING", "download",
                      f"local file gone: {p or '(no path)'} (was s3://{r['bucket']}/{r['key']})")
    log_event("INFO", "download",
              f"local integrity check: {checked} downloaded file(s), {deleted} now missing")
    return checked, deleted


def scan_orphans(root=None, limit=5000):
    """Files sitting in the download folder that no index row points at.

    They appear when the index is cleared (migrating buckets drops that bucket's
    `downloads` rows) or when a database is wiped — the bytes stay on disk,
    silently taking up space, with nothing in the app mentioning them.

    A downloaded file's path relative to the destination *is* its S3 key, so for
    each orphan we also look the key up across every indexed bucket. That answers
    the only question that matters before deleting one: is it still archived, or
    is this the last copy? Returns (orphans, totals)."""
    root = os.path.realpath(root or config.DOWNLOAD_DIR)
    known = db.downloaded_paths()
    found, tracked_n, tracked_b, truncated = [], 0, 0, False
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            p = os.path.join(dirpath, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if p in known or os.path.realpath(p) in known:
                tracked_n += 1
                tracked_b += st.st_size
                continue
            if len(found) >= limit:
                truncated = True
                continue
            found.append({"path": p, "key": os.path.relpath(p, root),
                          "size": st.st_size,
                          "mtime": datetime.fromtimestamp(st.st_mtime).strftime(db.TIME_FMT)})
    archived = db.find_keys_any_bucket([f["key"] for f in found])
    for f in found:
        a = archived.get(f["key"])
        f["archived_in"] = a["bucket"] if a else None
        f["archived_status"] = a["status"] if a else None
        # Same key, different size: the local copy is not what's in the archive.
        f["size_match"] = bool(a) and int(a["size"] or 0) == f["size"]
    found.sort(key=lambda f: -f["size"])
    return found, {
        "root": root,
        "orphans": len(found),
        "orphan_bytes": sum(f["size"] for f in found),
        "unarchived": sum(1 for f in found if not f["archived_in"]),
        "unarchived_bytes": sum(f["size"] for f in found if not f["archived_in"]),
        "tracked": tracked_n, "tracked_bytes": tracked_b,
        "truncated": truncated,
    }


def delete_local(paths, root=None):
    """Delete local files under the download folder, then prune the directories
    left empty. Refuses anything outside the folder. Never touches S3."""
    root = os.path.realpath(root or config.DOWNLOAD_DIR)
    deleted, freed, failed = 0, 0, []
    for p in paths:
        real = os.path.realpath(p)
        if real != root and not real.startswith(root.rstrip("/") + "/"):
            failed.append({"path": p, "error": "outside the download folder"})
            continue
        try:
            size = os.path.getsize(real)
            os.remove(real)
            deleted += 1
            freed += size
            log_event("INFO", "download", f"deleted local file {real} ({size:,} bytes)")
        except OSError as e:
            failed.append({"path": p, "error": str(e)})
    # tidy up directories the deletions emptied (never the root itself)
    for dirpath, _d, _f in sorted(os.walk(root), key=lambda t: -len(t[0])):
        if dirpath != root:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass
    return deleted, freed, failed


def plan_within_budget(items, budget):
    """Order-preserving greedy pack: keep each item whose size still fits the
    remaining budget, skip the ones that don't. Returns (fit, skipped, used)."""
    fit, skipped, used = [], [], 0
    for it in items:
        s = int(it.get("size") or 0)
        if used + s <= budget:
            fit.append(it)
            used += s
        else:
            skipped.append(it)
    return fit, skipped, used


def safe_dest(dest_root, key):
    """Map an S3 key to a local path under dest_root, refusing anything that
    would escape it (defensive against keys containing '..')."""
    target = os.path.realpath(os.path.join(dest_root, key))
    root = os.path.realpath(dest_root)
    if target != root and not target.startswith(root.rstrip("/") + "/"):
        raise ValueError(f"key would escape destination folder: {key}")
    return target


def _hash_file(path, extra_algo=None):
    """SHA-256 of the file, plus optionally a second digest (e.g. the offload
    manifest's xxh64) in the same read pass. Returns (sha_hex, extra_hex|None)."""
    h = hashlib.sha256()
    extra = manifest.new_hasher(extra_algo) if extra_algo else None
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(READ_BLOCK), b""):
            h.update(blk)
            if extra is not None:
                extra.update(blk)
    return h.hexdigest(), (extra.hexdigest() if extra is not None else None)


class Downloader:
    """One session at a time; files within a session download in parallel,
    and large files are fetched with parallel ranged GETs. Runs on the master
    and on every agent: the master creates sessions (with the item list stored
    on the session), and an agent runs the ones created for it."""

    def __init__(self):
        self.q = queue.Queue()
        self.current_session = None
        self.on_session_done = None   # optional callback(sid)
        threading.Thread(target=self._loop, daemon=True, name="downloader").start()

    def enqueue(self, dest, items, location=""):
        """Master: queue a download of `items` ([{"bucket", "key", "size",
        "sha256"}, …]) into `dest` on this machine, or on agent `location` —
        which picks it up at its next check-in."""
        items = [{k: i.get(k) for k in ("bucket", "key", "size", "sha256")} for i in items]
        sid = db.create_dl_session(dest, location=location, items=items)
        total_bytes = sum(int(i.get("size") or 0) for i in items)
        db.update_dl_session(sid, total_files=len(items), total_bytes=total_bytes)
        log_event("INFO", "download",
                  f"Download session #{sid} queued: {len(items)} object(s), "
                  f"{total_bytes:,} bytes -> {dest}" + (f" on {location}" if location else ""))
        if not location:
            self.q.put(sid)
        return sid

    def run_assigned(self, sid):
        """Agent: run a download session the master created for this machine."""
        self.q.put(sid)

    def queue_size(self):
        return self.q.qsize()

    def _loop(self):
        while True:
            sid = self.q.get()
            self.current_session = sid
            try:
                s = db.get_dl_session(sid)
                self._run(sid, s["dest"], json.loads(s["items"] or "[]"))
            except Exception as e:
                log_event("ERROR", "download", f"Download session #{sid} crashed: {e}")
                db.update_dl_session(sid, status="failed", finished_at=db.now())
            finally:
                self.current_session = None
                if self.on_session_done:
                    self.on_session_done(sid)

    def _run(self, sid, dest, items):
        db.update_dl_session(sid, status="running")
        with ThreadPoolExecutor(max_workers=config.DOWNLOAD_WORKERS) as ex:
            for fut in [ex.submit(self._one, sid, dest, it) for it in items]:
                fut.result()
        s = db.get_dl_session(sid)
        status = "done" if s["failed_files"] == 0 else "done_with_errors"
        db.update_dl_session(sid, status=status, finished_at=db.now())
        log_event("INFO", "download",
                  f"Download session #{sid} finished: {s['done_files']} downloaded, "
                  f"{s['skipped_files']} skipped, {s['failed_files']} failed")

    # ---- single object ----

    def _one(self, sid, dest, item):
        bucket, key = item["bucket"], item["key"]
        expected_sha = item.get("sha256")
        did = None
        try:
            path = safe_dest(dest, key)
            size = int(item.get("size") or 0)

            # already present locally and matching -> skip
            if os.path.exists(path):
                local_size = os.path.getsize(path)
                if size and local_size == size:
                    if not expected_sha or _hash_file(path)[0] == expected_sha:
                        skip_id = db.add_download(sid, bucket, key, path, size)
                        db.update_download(skip_id, status="skipped", finished_at=db.now())
                        db.bump_dl_session(sid, skipped=1, bytes_done=size)
                        log_event("DEBUG", "download",
                                  f"skip (already downloaded): {path}")
                        return

            head = awsapi.s3api("head-object", "--bucket", bucket, "--key", key, log=False)
            size = int(head.get("ContentLength") or size or 0)
            restore_hdr = head.get("Restore") or ""
            storage = head.get("StorageClass") or ""
            if storage in ("GLACIER", "DEEP_ARCHIVE"):
                if 'ongoing-request="true"' in restore_hdr:
                    raise AwsError("restore still in progress — not yet downloadable")
                if 'ongoing-request="false"' not in restore_hdr:
                    raise AwsError(f"object is in {storage} and not restored — "
                                   f"request a restore first")

            did = db.add_download(sid, bucket, key, path, size)
            os.makedirs(os.path.dirname(path), exist_ok=True)

            t0 = time.monotonic()
            if size and size > config.DOWNLOAD_PART_SIZE:
                self._ranged(sid, bucket, key, path, size)
            else:
                awsapi.s3api("get-object", "--bucket", bucket, "--key", key, path)
                db.bump_dl_session(sid, bytes_done=size)
            elapsed = max(time.monotonic() - t0, 1e-6)

            actual = os.path.getsize(path)
            if size and actual != size:
                raise AwsError(f"size mismatch: expected {size:,}, got {actual:,}")

            # Also re-verify against the offload manifest hash (xxh64/…) recorded
            # at upload, if any — computed in the same read pass as SHA-256. This
            # is a true byte-level check that the restored copy matches what the
            # DIT recorded at offload.
            frow = db.get_file(bucket, key) if config.MANIFEST_CHECK else None
            m_algo = (frow or {}).get("manifest_algo")
            m_expected = (frow or {}).get("manifest_hash")
            want = m_algo if m_expected else None

            sha, m_computed = _hash_file(path, want)
            if expected_sha and sha != expected_sha:
                raise AwsError(f"checksum mismatch: index {expected_sha[:16]}… "
                               f"vs downloaded {sha[:16]}…")

            m_state = None
            if m_expected:
                if m_computed is None:
                    m_state = "algo_unsupported"
                elif manifest.normalize_hash(m_algo, m_computed) == m_expected:
                    m_state = "ok"
                else:
                    m_state = "mismatch"
                    log_event("ERROR", "download",
                              f"MANIFEST MISMATCH on download: {path} — {m_algo} "
                              f"manifest={m_expected} computed={manifest.normalize_hash(m_algo, m_computed)}")

            status = "verified" if expected_sha else "downloaded"
            db.update_download(did, status=status, sha256=sha,
                               download_seconds=round(elapsed, 3),
                               finished_at=db.now(), manifest_state=m_state)
            db.bump_dl_session(sid, done=1)
            log_event("INFO", "download",
                      f"{status}: {path} ({actual:,} bytes in {elapsed:.1f}s, "
                      f"{fmt_speed(actual / elapsed)}"
                      + (f", sha256 matches index" if expected_sha else
                         ", no index checksum to verify against")
                      + (f", manifest {m_state}" if m_state else "") + ")")
        except Exception as e:
            if did:
                db.update_download(did, status="failed", error=str(e)[:1000],
                                   finished_at=db.now())
            else:
                fail_id = db.add_download(sid, bucket, key, "", item.get("size"))
                db.update_download(fail_id, status="failed", error=str(e)[:1000],
                                   finished_at=db.now())
            db.bump_dl_session(sid, failed=1)
            log_event("ERROR", "download", f"FAILED: s3://{bucket}/{key}", str(e)[:2000])

    def _ranged(self, sid, bucket, key, path, size):
        """Parallel ranged GETs written into a preallocated file with pwrite."""
        part = config.DOWNLOAD_PART_SIZE
        ranges = [(o, min(o + part, size) - 1) for o in range(0, size, part)]
        with open(path, "wb") as f:
            f.truncate(size)
        fd = os.open(path, os.O_WRONLY)
        done = {"n": 0}
        lock = threading.Lock()

        def fetch(idx, start, end):
            fdd, tmp = tempfile.mkstemp(dir=config.TMP_DIR, suffix=".dlpart")
            os.close(fdd)
            try:
                t0 = time.monotonic()
                awsapi.s3api("get-object", "--bucket", bucket, "--key", key,
                             "--range", f"bytes={start}-{end}", tmp, log=False)
                secs = max(time.monotonic() - t0, 1e-6)
                off = start
                with open(tmp, "rb") as pf:
                    for blk in iter(lambda: pf.read(READ_BLOCK), b""):
                        os.pwrite(fd, blk, off)
                        off += len(blk)
                got = off - start
                db.bump_dl_session(sid, bytes_done=got)
                with lock:
                    done["n"] += got
                    total = done["n"]
                log_event("INFO", "download",
                          f"ranged {key}: part {idx + 1}/{len(ranges)} "
                          f"({total:,}/{size:,} bytes, {100 * total // size}%, "
                          f"{fmt_speed(got / secs)})")
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

        try:
            with ThreadPoolExecutor(max_workers=config.DOWNLOAD_PART_WORKERS) as ex:
                for fut in [ex.submit(fetch, i, s, e) for i, (s, e) in enumerate(ranges)]:
                    fut.result()
        finally:
            os.close(fd)
