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


@app.get("/api/browse")
def api_browse():
    path = request.args.get("path") or (config.BROWSE_ROOTS[0] if config.BROWSE_ROOTS else "/media")
    if not _allowed_path(path):
        return jsonify({"error": f"path outside allowed roots ({', '.join(config.BROWSE_ROOTS)})"}), 403
    if not os.path.isdir(path):
        return jsonify({"error": "not a directory"}), 400
    dirs, nfiles = [], 0
    try:
        for e in sorted(os.scandir(path), key=lambda x: x.name.lower()):
            if e.is_dir(follow_symlinks=False):
                dirs.append(e.name)
            elif e.is_file(follow_symlinks=False):
                nfiles += 1
    except OSError as e:
        return jsonify({"error": str(e)}), 400
    parent = os.path.dirname(path.rstrip("/"))
    return jsonify({"path": path, "parent": parent if _allowed_path(parent) else None,
                    "dirs": dirs, "file_count": nfiles})


@app.post("/api/upload")
def api_upload():
    data = request.get_json(force=True)
    path = (data.get("path") or "").strip()
    if not path or not _allowed_path(path):
        return jsonify({"error": f"path must be inside: {', '.join(config.BROWSE_ROOTS)}"}), 400
    if not os.path.isdir(path):
        return jsonify({"error": "not a directory"}), 400
    label = (data.get("label") or "").strip() or os.path.basename(path.rstrip("/"))
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
    try:
        args = ["list-objects-v2", "--bucket", config.BUCKET]
        if config.PREFIX:
            args += ["--prefix", config.PREFIX + "/"]
        resp = awsapi.s3api(*args, timeout=600)
    except AwsError as e:
        return jsonify({"error": str(e)[:500]}), 502
    added = 0
    for obj in resp.get("Contents", []):
        if obj["Key"].endswith("/"):
            continue
        if not db.get_file(config.BUCKET, obj["Key"]):
            db.upsert_file(config.BUCKET, obj["Key"], size=obj.get("Size"),
                           etag=(obj.get("ETag") or "").strip('"'),
                           storage_class=obj.get("StorageClass"),
                           status="remote", uploaded_at=obj.get("LastModified"))
            added += 1
    log_event("INFO", "app", f"bucket sync: imported {added} objects not present in index")
    return jsonify({"imported": added, "listed": len(resp.get("Contents", []))})


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
