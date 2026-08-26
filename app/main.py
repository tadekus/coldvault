import os
import tempfile

from flask import Flask, jsonify, render_template, request

import awsapi
import config
import db
import downloader as downloader_mod
import editlist
import restore
import uploader as uploader_mod
import watcher as watcher_mod
from awsapi import AwsError
from logs import log_event

app = Flask(__name__)
up = uploader_mod.Uploader()
watch = watcher_mod.Watcher(up)
down = downloader_mod.Downloader()

# A bucket picked in the UI is persisted in the DB and overrides the .env value
_saved_bucket = db.get_setting("bucket")
if _saved_bucket:
    config.BUCKET = _saved_bucket

_stale = db.fail_stale_uploads()
if _stale:
    log_event("WARNING", "app",
              f"marked {_stale} upload(s) interrupted by restart as failed — "
              f"re-run their session to retry")
_stale_dl = db.fail_stale_downloads()
if _stale_dl:
    log_event("WARNING", "app",
              f"marked {_stale_dl} download(s) interrupted by restart as failed")


def _allowed_path(path):
    real = os.path.realpath(path)
    return any(real == r or real.startswith(r.rstrip("/") + "/")
               for r in config.BROWSE_ROOTS)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/status")
def api_status():
    return jsonify({
        "bucket": config.BUCKET,
        "region": os.environ.get("AWS_DEFAULT_REGION", ""),
        "prefix": config.PREFIX,
        "storage_class": config.STORAGE_CLASS,
        "watch_dirs": config.WATCH_DIRS,
        "canary": config.CANARY_NAME,
        "auto_upload": config.AUTO_UPLOAD,
        "active_mounts": watch.active,
        "queue_size": up.queue_size(),
        "current_session": up.current_session,
        "uploading": db.uploading_files(),
    })


@app.get("/api/stats")
def api_stats():
    return jsonify(db.stats(bucket=config.BUCKET))


@app.post("/api/test")
def api_test():
    try:
        ident = awsapi.aws("sts", "get-caller-identity")
        awsapi.s3api("head-bucket", "--bucket", config.BUCKET)
        log_event("INFO", "app", f"connection test OK (account {ident.get('Account')})")
        return jsonify({"ok": True, "account": ident.get("Account"), "arn": ident.get("Arn")})
    except AwsError as e:
        return jsonify({"ok": False, "error": str(e)[:500]}), 502


@app.get("/api/buckets")
def api_buckets():
    try:
        resp = awsapi.s3api("list-buckets")
    except AwsError as e:
        return jsonify({"error": str(e)[:500]}), 502
    buckets = [{"name": b["Name"], "created": b.get("CreationDate")}
               for b in resp.get("Buckets", [])]
    return jsonify({"buckets": buckets, "current": config.BUCKET})


@app.post("/api/bucket")
def api_set_bucket():
    name = (request.get_json(force=True).get("name") or "").strip()
    if not name:
        return jsonify({"error": "no bucket name given"}), 400
    try:
        awsapi.s3api("head-bucket", "--bucket", name)
    except AwsError as e:
        return jsonify({"error": f"bucket not accessible: {str(e)[:300]}"}), 400
    config.BUCKET = name
    db.set_setting("bucket", name)
    log_event("INFO", "app", f"active bucket switched to '{name}' (persisted, overrides .env)")
    return jsonify({"ok": True, "bucket": name})


@app.get("/api/browse/roots")
def api_browse_roots():
    """The configured upload roots (watch dirs + COLDVAULT_BROWSE_ROOTS), each
    checked for whether it actually exists and is readable inside the container.
    Deduplicated, order preserved."""
    seen, roots = set(), []
    for r in config.BROWSE_ROOTS:
        real = os.path.realpath(r)
        if real in seen:
            continue
        seen.add(real)
        exists = os.path.isdir(r)
        roots.append({
            "path": r,
            "exists": exists,
            "readable": exists and os.access(r, os.R_OK | os.X_OK),
            "is_watch": r in config.WATCH_DIRS,
        })
    return jsonify({"roots": roots})


@app.get("/api/browse")
def api_browse():
    path = request.args.get("path") or (config.BROWSE_ROOTS[0] if config.BROWSE_ROOTS else "/media")
    if not _allowed_path(path):
        return jsonify({"error": f"path outside allowed roots ({', '.join(config.BROWSE_ROOTS)})"}), 403
    if not os.path.isdir(path):
        return jsonify({"error": "not a directory"}), 400
    dirs, files, total_bytes, nfiles = [], [], 0, 0
    try:
        for e in sorted(os.scandir(path), key=lambda x: x.name.lower()):
            if e.is_dir(follow_symlinks=False):
                dirs.append(e.name)
            elif e.is_file(follow_symlinks=False):
                nfiles += 1
                try:
                    sz = e.stat(follow_symlinks=False).st_size
                except OSError:
                    sz = 0
                total_bytes += sz
                if len(files) < 500:
                    files.append({"name": e.name, "size": sz})
    except OSError as e:
        return jsonify({"error": str(e)}), 400
    parent = os.path.dirname(path.rstrip("/"))
    return jsonify({"path": path, "parent": parent if _allowed_path(parent) else None,
                    "dirs": dirs, "files": files, "file_count": nfiles,
                    "files_truncated": nfiles > len(files), "total_bytes": total_bytes})


@app.post("/api/upload")
def api_upload():
    data = request.get_json(force=True)
    label = (data.get("label") or "").strip()
    items = data.get("items")

    if items:  # explicit selection of files/folders
        clean = []
        for it in items:
            it = (it or "").strip()
            if not it or not _allowed_path(it):
                return jsonify({"error": f"item outside allowed roots: {it}"}), 400
            if not os.path.exists(it):
                return jsonify({"error": f"no longer exists: {it}"}), 400
            clean.append(it)
        if not clean:
            return jsonify({"error": "no items selected"}), 400
        sid = up.enqueue(None, label, "manual", items=clean)
        return jsonify({"session_id": sid})

    # whole folder
    path = (data.get("path") or "").strip()
    if not path or not _allowed_path(path):
        return jsonify({"error": f"path must be inside: {', '.join(config.BROWSE_ROOTS)}"}), 400
    if not os.path.isdir(path):
        return jsonify({"error": "not a directory"}), 400
    sid = up.enqueue(path, label, "manual")
    return jsonify({"session_id": sid})


@app.get("/api/files")
def api_files():
    # bucket param: absent -> active bucket, "*" -> all buckets, else that bucket
    bucket = request.args.get("bucket")
    if bucket is None or bucket == "":
        bucket = config.BUCKET
    elif bucket == "*":
        bucket = None
    total, total_bytes, items = db.search_files(
        bucket=bucket,
        q=request.args.get("q"),
        status=request.args.get("status") or None,
        session_id=request.args.get("session_id") or None,
        sort=request.args.get("sort") or "new",
        limit=request.args.get("limit", 100),
        offset=request.args.get("offset", 0),
    )
    restores = db.latest_restores_for_files(items)
    for i in items:
        r = restores.get((i["bucket"], i["key"]))
        i["restore"] = {"status": r["status"], "tier": r["tier"],
                        "expiry": r["expiry"]} if r else None
    return jsonify({"total": total, "total_bytes": total_bytes, "items": items,
                    "buckets": db.distinct_buckets(), "active": config.BUCKET})


@app.post("/api/editlist")
def api_editlist():
    """Upload an edit file (xmeml/fcpxml/AAF), extract referenced media names,
    and match them against the index for a batch restore."""
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify({"error": "no file uploaded"}), 400
    bucket = request.form.get("bucket") or config.BUCKET
    if bucket == "*":
        bucket = None
    fd, tmp = tempfile.mkstemp(dir=config.TMP_DIR, suffix=".editlist")
    os.close(fd)
    try:
        upload.save(tmp)
        fmt, refs = editlist.parse_edit(tmp, upload.filename)
    except Exception as e:
        return jsonify({"error": f"could not parse {upload.filename}: {e}"}), 400
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    matched, unmatched = [], []
    for name in sorted(refs):
        rows = db.match_files_by_name(bucket, name)
        if rows:
            matched.append({"ref": name, "source": refs[name], "files": rows})
        else:
            unmatched.append({"ref": name, "source": refs[name]})
    log_event("INFO", "editlist",
              f"parsed {upload.filename} ({fmt}): {len(refs)} media refs — "
              f"{len(matched)} matched in index, {len(unmatched)} not found"
              + (f" (bucket {bucket})" if bucket else " (all buckets)"))
    return jsonify({"format": fmt, "total_refs": len(refs),
                    "matched": matched, "unmatched": unmatched})


@app.get("/api/sessions")
def api_sessions():
    return jsonify(db.list_sessions())


@app.post("/api/restore")
def api_restore():
    data = request.get_json(force=True)
    items = data.get("items") or []
    if not items:
        return jsonify({"error": "no objects given"}), 400
    try:
        results = restore.request_restore(items, data.get("tier", "Bulk"),
                                          data.get("days", 7))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"results": results})


@app.get("/api/restores")
def api_restores():
    return jsonify(db.list_restores())


@app.post("/api/restores/refresh")
def api_restores_refresh():
    updated = restore.check_pending()
    return jsonify({"completed_now": updated})


@app.post("/api/sync")
def api_sync():
    """Import existing bucket objects into the index (status 'remote')."""
    added = listed = 0
    try:
        for obj in awsapi.list_all_objects(config.BUCKET, config.PREFIX):
            listed += 1
            if obj["Key"].endswith("/"):
                continue
            if not db.get_file(config.BUCKET, obj["Key"]):
                db.upsert_file(config.BUCKET, obj["Key"], size=obj.get("Size"),
                               etag=(obj.get("ETag") or "").strip('"'),
                               storage_class=obj.get("StorageClass"),
                               status="remote", uploaded_at=obj.get("LastModified"))
                added += 1
    except AwsError as e:
        return jsonify({"error": str(e)[:500]}), 502
    log_event("INFO", "app", f"bucket sync: imported {added} of {listed} listed objects")
    return jsonify({"imported": added, "listed": listed})


@app.post("/api/audit")
def api_audit():
    """Reconcile the index against the actual bucket contents: flag objects the
    index believes are archived but are missing from S3, size mismatches, and
    storage-class drift; import objects present in the bucket but not indexed."""
    bucket = config.BUCKET
    try:
        remote = {}
        for obj in awsapi.list_all_objects(bucket, config.PREFIX):
            if not obj["Key"].endswith("/"):
                remote[obj["Key"]] = obj
    except AwsError as e:
        return jsonify({"error": str(e)[:500]}), 502

    db.clear_audit(bucket)
    when = db.now()
    indexed = db.files_for_audit(bucket)
    indexed_keys = {r["key"] for r in indexed}
    missing, size_mismatch, class_drift, ok = [], [], [], 0

    for r in indexed:
        obj = remote.get(r["key"])
        if obj is None:
            db.set_audit(r["id"], "missing", when)
            missing.append(r["key"])
        elif r["size"] is not None and obj.get("Size") is not None \
                and int(r["size"]) != int(obj["Size"]):
            db.set_audit(r["id"], "size_mismatch", when)
            size_mismatch.append({"key": r["key"], "index": r["size"], "bucket": obj["Size"]})
        elif obj.get("StorageClass") and obj["StorageClass"] != config.STORAGE_CLASS:
            db.set_audit(r["id"], "class_drift", when)
            class_drift.append({"key": r["key"], "class": obj["StorageClass"]})
        else:
            db.set_audit(r["id"], "ok", when)
            ok += 1

    imported = 0
    for key, obj in remote.items():
        if key not in indexed_keys and not db.get_file(bucket, key):
            db.upsert_file(bucket, key, size=obj.get("Size"),
                           etag=(obj.get("ETag") or "").strip('"'),
                           storage_class=obj.get("StorageClass"),
                           status="remote", uploaded_at=obj.get("LastModified"),
                           audit_state="ok", audited_at=when)
            imported += 1

    level = "ERROR" if missing else ("WARNING" if (size_mismatch or class_drift) else "INFO")
    log_event(level, "audit",
              f"bucket audit of s3://{bucket}: {ok} ok, {len(missing)} MISSING, "
              f"{len(size_mismatch)} size mismatch, {len(class_drift)} class drift, "
              f"{imported} imported ({len(remote)} objects in bucket)")
    for k in missing:
        log_event("ERROR", "audit", f"MISSING from bucket (index says archived): {k}")
    for m in size_mismatch:
        log_event("WARNING", "audit",
                  f"size mismatch: {m['key']} index={m['index']} bucket={m['bucket']}")

    return jsonify({"bucket": bucket, "in_bucket": len(remote), "in_index": len(indexed),
                    "ok": ok, "missing": missing[:200], "missing_count": len(missing),
                    "size_mismatch": size_mismatch[:200], "size_mismatch_count": len(size_mismatch),
                    "class_drift": class_drift[:200], "class_drift_count": len(class_drift),
                    "imported": imported})


def _allowed_dest(path):
    real = os.path.realpath(path)
    root = os.path.realpath(config.DOWNLOAD_DIR)
    return real == root or real.startswith(root.rstrip("/") + "/")


@app.get("/api/restored")
def api_restored():
    """Objects whose latest restore completed — i.e. downloadable right now."""
    items = db.restored_objects()
    downloaded = db.completed_downloads_map()
    for i in items:
        i["downloaded_to"] = downloaded.get((i["bucket"], i["key"]))
    return jsonify({"items": items, "download_dir": config.DOWNLOAD_DIR})


@app.get("/api/download/browse")
def api_download_browse():
    path = request.args.get("path") or config.DOWNLOAD_DIR
    if not _allowed_dest(path):
        return jsonify({"error": f"path must be inside {config.DOWNLOAD_DIR}"}), 403
    if not os.path.isdir(path):
        return jsonify({"error": "not a directory"}), 400
    try:
        dirs = sorted(e.name for e in os.scandir(path) if e.is_dir(follow_symlinks=False))
    except OSError as e:
        return jsonify({"error": str(e)}), 400
    parent = os.path.dirname(path.rstrip("/"))
    return jsonify({"path": path, "parent": parent if _allowed_dest(parent) else None,
                    "dirs": dirs})


@app.post("/api/download")
def api_download():
    data = request.get_json(force=True)
    dest = (data.get("dest") or config.DOWNLOAD_DIR).strip()
    items = data.get("items") or []
    if not items:
        return jsonify({"error": "no objects selected"}), 400
    if not _allowed_dest(dest):
        return jsonify({"error": f"destination must be inside {config.DOWNLOAD_DIR}"}), 400
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError as e:
        return jsonify({"error": f"cannot create destination: {e}"}), 400
    if not os.access(dest, os.W_OK):
        return jsonify({"error": f"destination not writable: {dest} — check the "
                                 f"volume mount is read-write"}), 400
    sid = down.enqueue(dest, items)
    return jsonify({"session_id": sid})


@app.get("/api/download/sessions")
def api_download_sessions():
    return jsonify({"sessions": db.list_dl_sessions(),
                    "files": db.list_downloads(limit=300),
                    "queue_size": down.queue_size(),
                    "current_session": down.current_session})


@app.get("/api/logs")
def api_logs():
    return jsonify(db.list_events(
        level=request.args.get("level") or None,
        category=request.args.get("category") or None,
        q=request.args.get("q") or None,
        limit=request.args.get("limit", 200),
    ))


if __name__ == "__main__":
    watch.start()
    restore.start_poller()
    log_event("INFO", "app",
              f"ColdVault started — bucket={config.BUCKET or '(not set!)'}, "
              f"storage_class={config.STORAGE_CLASS}, port={config.PORT}")
    app.run(host="0.0.0.0", port=config.PORT, threaded=True)
