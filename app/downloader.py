"""Download restored objects back to a local folder, preserving the S3 key
structure as directories, and verify each file against the SHA-256 recorded
in the index when it was uploaded."""
import hashlib
import os
import queue
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import awsapi
import config
import db
from awsapi import AwsError
from logs import log_event

READ_BLOCK = 8 * 1024 * 1024


def fmt_speed(bps):
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bps < 1024 or unit == "GB/s":
            return f"{bps:.1f} {unit}"
        bps /= 1024


def safe_dest(dest_root, key):
    """Map an S3 key to a local path under dest_root, refusing anything that
    would escape it (defensive against keys containing '..')."""
    target = os.path.realpath(os.path.join(dest_root, key))
    root = os.path.realpath(dest_root)
    if target != root and not target.startswith(root.rstrip("/") + "/"):
        raise ValueError(f"key would escape destination folder: {key}")
    return target


def _hash_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(READ_BLOCK), b""):
            h.update(blk)
    return h.hexdigest()


class Downloader:
    """One session at a time; files within a session download in parallel,
    and large files are fetched with parallel ranged GETs."""

    def __init__(self):
        self.q = queue.Queue()
        self.current_session = None
        threading.Thread(target=self._loop, daemon=True, name="downloader").start()

    def enqueue(self, dest, items):
        """items: [{"bucket":…, "key":…, "size":…, "sha256":…}, …]"""
        sid = db.create_dl_session(dest)
        total_bytes = sum(int(i.get("size") or 0) for i in items)
        db.update_dl_session(sid, total_files=len(items), total_bytes=total_bytes)
        log_event("INFO", "download",
                  f"Download session #{sid} queued: {len(items)} object(s), "
                  f"{total_bytes:,} bytes -> {dest}")
        self.q.put((sid, dest, items))
        return sid

    def queue_size(self):
        return self.q.qsize()

    def _loop(self):
        while True:
            sid, dest, items = self.q.get()
            self.current_session = sid
            try:
                self._run(sid, dest, items)
            except Exception as e:
                log_event("ERROR", "download", f"Download session #{sid} crashed: {e}")
                db.update_dl_session(sid, status="failed", finished_at=db.now())
            finally:
                self.current_session = None

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
                    if not expected_sha or _hash_file(path) == expected_sha:
                        db.add_download(sid, bucket, key, path, size)
                        db.update_download(db.list_downloads(sid, 1)[0]["id"],
                                           status="skipped", finished_at=db.now())
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

            sha = _hash_file(path)
            if expected_sha and sha != expected_sha:
                raise AwsError(f"checksum mismatch: index {expected_sha[:16]}… "
                               f"vs downloaded {sha[:16]}…")
            status = "verified" if expected_sha else "downloaded"
            db.update_download(did, status=status, sha256=sha,
                               download_seconds=round(elapsed, 3),
                               finished_at=db.now())
            db.bump_dl_session(sid, done=1)
            log_event("INFO", "download",
                      f"{status}: {path} ({actual:,} bytes in {elapsed:.1f}s, "
                      f"{fmt_speed(actual / elapsed)}"
                      + (f", sha256 matches index" if expected_sha else
                         ", no index checksum to verify against") + ")")
        except Exception as e:
            if did:
                db.update_download(did, status="failed", error=str(e)[:1000],
                                   finished_at=db.now())
            else:
                db.add_download(sid, bucket, key, "", item.get("size"))
                db.update_download(db.list_downloads(sid, 1)[0]["id"],
                                   status="failed", error=str(e)[:1000],
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
