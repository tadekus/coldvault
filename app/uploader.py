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
import manifest
from awsapi import AwsError
from logs import log_event
from store import db

READ_BLOCK = 8 * 1024 * 1024


def _excluded(name):
    return any(fnmatch(name, pat) for pat in config.EXCLUDES)


def make_key(label, rel):
    parts = [config.PREFIX, (label or "").strip("/"), rel]
    return "/".join(p for p in parts if p)


def _left_out(name):
    return _excluded(name) or name == config.CANARY_NAME


def scan(root, skipped=None):
    """Every file an upload of folder `root` includes, as (path, size, mtime).
    Excluded names (COLDVAULT_EXCLUDES, the canary file) are left out; when
    `skipped` is a list, they are recorded there (folders with a trailing /),
    which is what the upload preview shows."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        keep = []
        for d in dirnames:
            if _excluded(d):
                if skipped is not None:
                    skipped.append(os.path.join(dirpath, d) + "/")
            else:
                keep.append(d)
        dirnames[:] = keep
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            if _left_out(fn):
                if skipped is not None:
                    skipped.append(p)
                continue
            try:
                st = os.stat(p)
            except OSError:
                continue
            if not os.path.isfile(p):
                continue
            found.append((p, st.st_size, st.st_mtime))
    return found


def scan_items(items, skipped=None):
    """An explicit selection: folders are walked, files taken as they are."""
    found = []
    for it in items:
        if os.path.isdir(it):
            found.extend(scan(it, skipped))
        elif os.path.isfile(it):
            if _left_out(os.path.basename(it)):
                if skipped is not None:
                    skipped.append(it)
                continue
            try:
                st = os.stat(it)
            except OSError:
                continue
            found.append((it, st.st_size, st.st_mtime))
    return found


def fmt_speed(bps):
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bps < 1000 or unit == "GB/s":   # decimal (SI), as transfer tools report
            return f"{bps:.1f} {unit}"
        bps /= 1000


def _hash_file(path, extra_algo=None):
    """Compute the upload SHA-256 in one read pass, and optionally a second
    digest (e.g. xxh64 for a manifest check) in the same pass. Returns
    (sha_hex, sha_b64, extra_hex_or_None)."""
    h = hashlib.sha256()
    extra = manifest.new_hasher(extra_algo) if extra_algo else None
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(READ_BLOCK), b""):
            h.update(blk)
            if extra is not None:
                extra.update(blk)
    return (h.hexdigest(), base64.b64encode(h.digest()).decode(),
            extra.hexdigest() if extra is not None else None)


class Uploader:
    """Serial session queue; files within a session upload in parallel workers.

    Runs on the master and on every agent. Each session is pinned to its bucket
    and to the machine (location) that holds its files; the master creates
    sessions, and an agent runs the ones created for it (run_assigned).

    Before each upload the file's S3 key is reserved (db.claim_upload), so two
    machines never write the same object at once: the second waits, then skips
    if the first archived the same content.

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
        self._stopped = set()   # sessions the master no longer has running
        self._mstats = {}       # sid -> {manifest_state: count}
        self._mlock = threading.Lock()
        # Optional callback(sid, trigger) run after a session finishes; set by main.
        self.on_session_done = None
        threading.Thread(target=self._loop, daemon=True, name="uploader").start()

    def enqueue(self, source, label, trigger, items=None, bucket=None):
        """Master: queue an upload of the folder `source`, or of just `items`
        under it (as localfs.prepare_upload returns them). `bucket` pins this
        session's destination (a canary's own bucket, or the one picked for a
        manual upload); None means the active bucket. Callers are expected to
        have validated it — see buckets.check()."""
        if not label:
            label = os.path.basename(source.rstrip("/")) or "upload"
        bucket = bucket or config.BUCKET
        sid = db.create_session(bucket, source, label, trigger, items=items)
        what = f"{len(items)} selected item(s) under {source}" if items else source
        log_event("INFO", "upload",
                  f"Session #{sid} queued: {what} -> s3://{bucket} "
                  f"(label='{label}', trigger={trigger})")
        self.q.put(sid)
        return sid

    def run_assigned(self, sid):
        """Agent: run a session the master created for this machine."""
        self.q.put(sid)

    def queue_size(self):
        return self.q.qsize()

    def _loop(self):
        while True:
            sid = self.q.get()
            self.current_session = sid
            trigger = None
            try:
                s = db.get_session(sid)
                trigger = s["trigger"] if s else None
                self._run_session(sid)
            except Exception as e:
                log_event("ERROR", "upload", f"Session #{sid} crashed: {e}")
                db.update_session(sid, status="failed", finished_at=db.now())
            finally:
                self.current_session = None
            if self.on_session_done:
                try:
                    self.on_session_done(sid, trigger)
                except Exception as e:
                    log_event("ERROR", "upload", f"on_session_done hook failed for #{sid}: {e}")

    # ---- session ----

    def _run_session(self, sid):
        s = db.get_session(sid)
        root, label, bucket = s["source"], s["label"], s["bucket"]
        sel = json.loads(s["items"]) if s.get("items") else None
        db.update_session(sid, status="scanning")
        files = scan_items(sel) if sel else scan(root)
        total_bytes = sum(f[1] for f in files)
        db.update_session(sid, total_files=len(files), total_bytes=total_bytes, status="running")
        log_event("INFO", "upload",
                  f"Session #{sid}: scanning done — {len(files)} files, "
                  f"{total_bytes:,} bytes under {root} -> s3://{bucket}")

        # Checksum manifests (CSV / ASC MHL from the DIT offload) for the integrity check.
        m_entries, m_found = {}, []
        if config.MANIFEST_CHECK:
            m_entries, m_found = manifest.load_manifests(root, config.MANIFEST_EXTS)
            if not m_found:
                log_event("INFO", "manifest",
                          f"Session #{sid}: no checksum manifest (.csv/.mhl) found under {root}")
        with self._mlock:
            self._mstats[sid] = {}

        with ThreadPoolExecutor(max_workers=config.UPLOAD_WORKERS) as ex:
            futures = [ex.submit(self._upload_one, sid, bucket, root, label, p, size, mtime, m_entries)
                       for p, size, mtime in files]
            for f in futures:
                f.result()

        if sid in self._stopped:
            self._stopped.discard(sid)
            log_event("WARNING", "upload",
                      f"Session #{sid} stopped: the master no longer has it running "
                      f"(it was failed while this machine was out of touch) — re-run it; "
                      f"verified files are skipped")
            return
        s = db.get_session(sid)
        status = "done" if s["failed_files"] == 0 else "done_with_errors"
        db.update_session(sid, status=status, finished_at=db.now())
        log_event("INFO", "upload",
                  f"Session #{sid} finished: {s['done_files']} uploaded, "
                  f"{s['skipped_files']} skipped, {s['failed_files']} failed")
        with self._mlock:
            ms = self._mstats.pop(sid, {})
        if m_found:
            lvl = "ERROR" if (ms.get("mismatch") or ms.get("algo_unsupported")) else "INFO"
            log_event(lvl, "manifest",
                      f"Session #{sid} manifest check: {ms.get('ok', 0)} ok, "
                      f"{ms.get('mismatch', 0)} mismatch, {ms.get('not_in_manifest', 0)} not in manifest, "
                      f"{ms.get('algo_unsupported', 0)} unsupported algorithm "
                      f"({len(m_found)} manifest file(s))")

    # ---- single file ----

    def _claim(self, sid, bucket, key, sha_hex, size, **fields):
        """Reserve `key` for this session just before uploading it. Returns
        True to go ahead; False when there's nothing to upload after all
        (another session just archived the same content — counted as skipped)
        or this session is no longer running (it stops)."""
        waited = False
        while True:
            c = db.claim_upload(bucket, key, sid, sha256=sha_hex, size=size, **fields)
            if c["state"] == "conflict":
                raise AwsError(f"s3://{bucket}/{key} already holds different content uploaded "
                               f"from {c['location'] or 'the master'} — two drives with the same "
                               f"label? Upload this one under another label (canary \"name\"). "
                               f"Nothing was overwritten.")
            if c["state"] == "ok":
                if waited:
                    log_event("INFO", "upload", f"s3://{bucket}/{key} is free again — uploading")
                return True
            if c["state"] == "inactive":
                if sid not in self._stopped:
                    self._stopped.add(sid)
                    log_event("WARNING", "upload",
                              f"Session #{sid} is no longer running on the master — stopping")
                return False
            if not waited:
                log_event("INFO", "upload",
                          f"waiting: s3://{bucket}/{key} is being uploaded by session "
                          f"#{c['holder']} on {c['location'] or 'the master'}")
                waited = True
            time.sleep(10)
            if sid in self._stopped:
                return False
            cur = db.get_file(bucket, key)
            if sha_hex and cur and cur["status"] == "verified" and cur["sha256"] == sha_hex:
                db.bump_session(sid, skipped=1, bytes_done=size)
                log_event("INFO", "upload",
                          f"skip (the other session archived identical content): s3://{bucket}/{key}")
                return False

    def _upload_one(self, sid, bucket, root, label, path, size, mtime, m_entries=None):
        if sid in self._stopped:
            return
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        key = make_key(label, rel)

        # Manifest cross-check: is this file listed in an offload checksum manifest?
        mentry = manifest.lookup(m_entries, path) if m_entries else None
        want_algo = mentry[0] if mentry else None
        m_state = m_algo = m_hash = None
        existing, claimed = None, False
        try:
            existing = db.get_file(bucket, key)
            if (existing and existing["status"] == "verified"
                    and existing["size"] == size
                    and abs((existing["mtime"] or 0) - mtime) < 2):
                db.bump_session(sid, skipped=1, bytes_done=size)
                log_event("DEBUG", "upload", f"skip (unchanged, already verified): s3://{bucket}/{key}")
                return

            sha_hex = sha_b64 = m_computed = None
            if config.DEDUPE or size <= config.MULTIPART_THRESHOLD or want_algo:
                sha_hex, sha_b64, m_computed = _hash_file(path, want_algo)

                # Evaluate the manifest check now that we've read the file.
                if mentry:
                    algo, expected, msize = mentry
                    m_algo, m_hash = algo, expected
                    if m_computed is None:
                        m_state = "algo_unsupported"
                        log_event("WARNING", "manifest",
                                  f"manifest hash '{algo}' not supported (install xxhash?): {rel}")
                    elif manifest.normalize_hash(algo, m_computed) == expected:
                        m_state = "ok"
                    else:
                        m_state = "mismatch"
                        log_event("ERROR", "manifest",
                                  f"MANIFEST MISMATCH: {rel} — {algo} manifest={expected} "
                                  f"computed={manifest.normalize_hash(algo, m_computed)}")
                elif m_entries:
                    m_state = "not_in_manifest"

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

            if not self._claim(sid, bucket, key, sha_hex, size, local_path=path,
                               mtime=mtime, storage_class=config.STORAGE_CLASS):
                return
            claimed = True

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
                           upload_seconds=round(elapsed, 3),
                           manifest_state=m_state, manifest_algo=m_algo, manifest_hash=m_hash)
            if m_state:
                with self._mlock:
                    st = self._mstats.setdefault(sid, {})
                    st[m_state] = st.get(m_state, 0) + 1
            db.bump_session(sid, done=1, bytes_done=bytes_to_credit)
            log_event("INFO", "upload",
                      f"verified: s3://{bucket}/{key} ({size:,} bytes in {elapsed:.1f}s, "
                      f"{fmt_speed(size / elapsed)}, sha256={hex_sha[:16]}…"
                      + (f", manifest {m_state}" if m_state and m_state != "not_in_manifest" else "") + ")")
        except Exception as e:
            # Only mark the index row failed if this session owns it: a read
            # error here mustn't turn another session's verified object into
            # "failed" — that object is still safely in S3.
            if claimed or not existing:
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
