import base64
import hashlib
import json
import os
import queue
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from fnmatch import fnmatch

import awsapi
import config
import db
from awsapi import AwsError
from logs import log_event

READ_BLOCK = 8 * 1024 * 1024


def _excluded(name):
    return any(fnmatch(name, pat) for pat in config.EXCLUDES)


def make_key(label, rel):
    parts = [config.PREFIX, (label or "").strip("/"), rel]
    return "/".join(p for p in parts if p)


def fmt_speed(bps):
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bps < 1024 or unit == "GB/s":
            return f"{bps:.1f} {unit}"
        bps /= 1024


def _hash_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(READ_BLOCK), b""):
            h.update(blk)
    return h.hexdigest(), base64.b64encode(h.digest()).decode()


class Uploader:
    """Serial session queue; files within a session upload in parallel workers.

    Each session is pinned to the bucket that was active when it was queued.

    Verification model: every upload sends --checksum-sha256, so S3 rejects the
    write on any corruption in transit. The checksum S3 returns is compared to
    the locally computed one before a file is marked 'verified'.

    Dedupe (three levels, per bucket):
      1. same key, verified, same size+mtime -> skip without reading the file
      2. same key, verified, same SHA-256    -> skip (file was touched/copied)
      3. COLDVAULT_DEDUPE: any verified object with identical SHA-256+size
         under a different key -> skip, logging what it duplicates
    """

    def __init__(self):
        self.q = queue.Queue()
        self.current_session = None
        threading.Thread(target=self._loop, daemon=True, name="uploader").start()

    def enqueue(self, path, label, trigger):
        sid = db.create_session(config.BUCKET, path, label, trigger)
        log_event("INFO", "upload",
                  f"Session #{sid} queued: {path} -> s3://{config.BUCKET} "
                  f"(label='{label}', trigger={trigger})")
        self.q.put(sid)
        return sid

    def queue_size(self):
        return self.q.qsize()

    def _loop(self):
        while True:
            sid = self.q.get()
            self.current_session = sid
            try:
                self._run_session(sid)
            except Exception as e:
                log_event("ERROR", "upload", f"Session #{sid} crashed: {e}")
                db.update_session(sid, status="failed", finished_at=db.now())
            finally:
                self.current_session = None

    # ---- session ----

    def _scan(self, root):
        found = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not _excluded(d)]
            for fn in filenames:
                if _excluded(fn) or fn == config.CANARY_NAME:
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if not os.path.isfile(p):
                    continue
                found.append((p, st.st_size, st.st_mtime))
        return found

    def _run_session(self, sid):
        s = db.get_session(sid)
        root, label, bucket = s["source"], s["label"], s["bucket"]
        db.update_session(sid, status="scanning")
        files = self._scan(root)
        total_bytes = sum(f[1] for f in files)
        db.update_session(sid, total_files=len(files), total_bytes=total_bytes, status="running")
        log_event("INFO", "upload",
                  f"Session #{sid}: scanning done — {len(files)} files, "
                  f"{total_bytes:,} bytes under {root} -> s3://{bucket}")

        with ThreadPoolExecutor(max_workers=config.UPLOAD_WORKERS) as ex:
            futures = [ex.submit(self._upload_one, sid, bucket, root, label, p, size, mtime)
                       for p, size, mtime in files]
            for f in futures:
                f.result()

        s = db.get_session(sid)
        status = "done" if s["failed_files"] == 0 else "done_with_errors"
        db.update_session(sid, status=status, finished_at=db.now())
        log_event("INFO", "upload",
                  f"Session #{sid} finished: {s['done_files']} uploaded, "
                  f"{s['skipped_files']} skipped, {s['failed_files']} failed")

    # ---- single file ----

    def _upload_one(self, sid, bucket, root, label, path, size, mtime):
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        key = make_key(label, rel)
        try:
            existing = db.get_file(bucket, key)
            if (existing and existing["status"] == "verified"
                    and existing["size"] == size
                    and abs((existing["mtime"] or 0) - mtime) < 2):
                db.bump_session(sid, skipped=1, bytes_done=size)
                log_event("DEBUG", "upload", f"skip (unchanged, already verified): s3://{bucket}/{key}")
                return

            sha_hex = sha_b64 = None
            if config.DEDUPE or size <= config.MULTIPART_THRESHOLD:
                sha_hex, sha_b64 = _hash_file(path)

                if (existing and existing["status"] == "verified"
                        and existing["sha256"] == sha_hex):
                    db.upsert_file(bucket, key, mtime=mtime)
                    db.bump_session(sid, skipped=1, bytes_done=size)
                    log_event("INFO", "upload",
                              f"skip (same content, mtime changed): s3://{bucket}/{key}")
                    return

                if config.DEDUPE:
                    dup = db.find_duplicate(bucket, sha_hex, size, exclude_key=key)
                    if dup:
                        db.bump_session(sid, skipped=1, bytes_done=size)
                        log_event("INFO", "upload",
                                  f"skip (duplicate content): {rel} is identical to "
                                  f"already-archived s3://{bucket}/{dup['key']}")
                        return

            db.upsert_file(bucket, key, local_path=path, size=size, mtime=mtime,
                           session_id=sid, status="uploading", error=None,
                           storage_class=config.STORAGE_CLASS)

            t0 = time.monotonic()
            if size <= config.MULTIPART_THRESHOLD:
                resp = self._put_object(bucket, path, key, sha_b64)
                hex_sha, expected = sha_hex, sha_b64
                bytes_to_credit = size
            else:
                # multipart credits session bytes per part as it goes
                hex_sha, expected, resp = self._multipart(bucket, path, key, sid, size)
                bytes_to_credit = 0
            elapsed = max(time.monotonic() - t0, 1e-6)

            remote = (resp.get("ChecksumSHA256") or "").strip('"')
            etag = (resp.get("ETag") or "").strip('"')
            if remote and remote != expected:
                raise AwsError(f"checksum mismatch: local {expected} vs S3 {remote}")

            db.upsert_file(bucket, key, sha256=hex_sha, checksum_s3=remote or expected,
                           etag=etag, status="verified",
                           uploaded_at=db.now(), verified_at=db.now(),
                           upload_seconds=round(elapsed, 3))
            db.bump_session(sid, done=1, bytes_done=bytes_to_credit)
            log_event("INFO", "upload",
                      f"verified: s3://{bucket}/{key} ({size:,} bytes in {elapsed:.1f}s, "
                      f"{fmt_speed(size / elapsed)}, sha256={hex_sha[:16]}…)")
        except Exception as e:
            db.upsert_file(bucket, key, status="failed", error=str(e)[:1000])
            db.bump_session(sid, failed=1)
            log_event("ERROR", "upload", f"FAILED: s3://{bucket}/{key}", str(e)[:2000])

    def _put_object(self, bucket, path, key, sha_b64):
        return awsapi.s3api(
            "put-object",
            "--bucket", bucket, "--key", key,
            "--body", path,
            "--storage-class", config.STORAGE_CLASS,
            "--checksum-sha256", sha_b64,
        )

    def _multipart(self, bucket, path, key, sid, size):
        """s3api multipart upload with per-part SHA-256 checksums.
        Parts are staged to temp files by one sequential read pass (which also
        feeds the whole-file hash) and uploaded by COLDVAULT_PART_WORKERS
        parallel upload-part calls — the same trick the CRT transfer client
        uses to get past single-stream throughput. A semaphore caps staged
        temp files at PART_WORKERS so disk usage stays bounded.
        Expected final checksum is the composite: sha256 over the concatenated
        raw part digests, suffixed with -<part count>.
        Session byte progress is credited after every part (and rolled back
        on failure) so the UI shows movement during large files."""
        resp = awsapi.s3api(
            "create-multipart-upload",
            "--bucket", bucket, "--key", key,
            "--storage-class", config.STORAGE_CLASS,
            "--checksum-algorithm", "SHA256",
        )
        upload_id = resp["UploadId"]
        whole = hashlib.sha256()
        digests, futures = [], []
        lock = threading.Lock()
        state = {"uploaded": 0}
        sem = threading.Semaphore(config.PART_WORKERS)

        def _send_part(part_no, tmp, part_b64, wrote):
            try:
                pt0 = time.monotonic()
                presp = awsapi.s3api(
                    "upload-part",
                    "--bucket", bucket, "--key", key,
                    "--upload-id", upload_id,
                    "--part-number", str(part_no),
                    "--body", tmp,
                    "--checksum-sha256", part_b64,
                )
                part_secs = max(time.monotonic() - pt0, 1e-6)
                with lock:
                    state["uploaded"] += wrote
                    done = state["uploaded"]
                db.bump_session(sid, bytes_done=wrote)
                log_event("INFO", "upload",
                          f"multipart {key}: part {part_no} done "
                          f"({done:,}/{size:,} bytes, {100 * done // size}%, "
                          f"{fmt_speed(wrote / part_secs)})")
                return {"PartNumber": part_no,
                        "ETag": presp["ETag"].strip('"'),
                        "ChecksumSHA256": part_b64}
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                sem.release()

        try:
            with ThreadPoolExecutor(max_workers=config.PART_WORKERS) as ex:
                with open(path, "rb") as f:
                    part_no = 0
                    while True:
                        part_no += 1
                        sem.acquire()
                        ph = hashlib.sha256()
                        wrote = 0
                        fd, tmp = tempfile.mkstemp(dir=config.TMP_DIR, suffix=".part")
                        try:
                            with os.fdopen(fd, "wb") as tf:
                                remaining = config.PART_SIZE
                                while remaining > 0:
                                    blk = f.read(min(READ_BLOCK, remaining))
                                    if not blk:
                                        break
                                    tf.write(blk)
                                    ph.update(blk)
                                    whole.update(blk)
                                    wrote += len(blk)
                                    remaining -= len(blk)
                        except Exception:
                            sem.release()
                            try:
                                os.unlink(tmp)
                            except OSError:
                                pass
                            raise
                        if wrote == 0:
                            sem.release()
                            try:
                                os.unlink(tmp)
                            except OSError:
                                pass
                            break
                        part_b64 = base64.b64encode(ph.digest()).decode()
                        digests.append(ph.digest())
                        futures.append(ex.submit(_send_part, part_no, tmp, part_b64, wrote))
                        if wrote < config.PART_SIZE:
                            break
                parts = [fut.result() for fut in futures]

            jfd, jpath = tempfile.mkstemp(dir=config.TMP_DIR, suffix=".json")
            try:
                with os.fdopen(jfd, "w") as jf:
                    json.dump({"Parts": parts}, jf)
                cresp = awsapi.s3api(
                    "complete-multipart-upload",
                    "--bucket", bucket, "--key", key,
                    "--upload-id", upload_id,
                    "--multipart-upload", f"file://{jpath}",
                )
            finally:
                try:
                    os.unlink(jpath)
                except OSError:
                    pass

            composite = base64.b64encode(
                hashlib.sha256(b"".join(digests)).digest()).decode() + f"-{len(parts)}"
            return whole.hexdigest(), composite, cresp
        except Exception:
            db.bump_session(sid, bytes_done=-state["uploaded"])
            awsapi.quiet_abort(bucket, key, upload_id)
            raise
