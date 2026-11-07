import config

# An agent (a second machine, COLDVAULT_ROLE=agent) runs a different, much
# smaller program and must never open a database — so decide before anything
# below imports one.
if config.ROLE == "agent":
    import agent
    agent.main()
    raise SystemExit(0)

import os  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from datetime import datetime  # noqa: E402

from flask import Flask, Response, jsonify, render_template, request  # noqa: E402

import agents as agents_mod  # noqa: E402
import audit as audit_mod  # noqa: E402
import awsapi  # noqa: E402
import backup as backup_mod  # noqa: E402
import buckets as buckets_mod  # noqa: E402
import db  # noqa: E402
import downloader as downloader_mod  # noqa: E402
import drives  # noqa: E402
import eject  # noqa: E402
import editlist  # noqa: E402
import localfs  # noqa: E402
import manifest  # noqa: E402
import manifest_verify  # noqa: E402
import notify  # noqa: E402
import restore  # noqa: E402
import retention  # noqa: E402
import treeexport  # noqa: E402
import upload_plan  # noqa: E402
import uploader as uploader_mod  # noqa: E402
import version  # noqa: E402
import watcher as watcher_mod  # noqa: E402
from awsapi import AwsError  # noqa: E402
from logs import log_event  # noqa: E402

app = Flask(__name__)
START_TIME = time.time()
up = uploader_mod.Uploader()
down = downloader_mod.Downloader()


# Sessions followed by an audit + report email (when COLDVAULT_NOTIFY is on):
# drive auto-uploads and uploads started by hand from the Upload tab.
EMAIL_TRIGGERS = ("canary", "manual")


def _after_upload(sid, trigger):
    """A session this master ran itself has finished."""
    s = db.get_session(sid)
    if not s or s["status"] == "stopped":
        return
    if trigger == "canary":
        eject.after_session(db.get_session(sid), busy=up.queue_size() > 0)
    if trigger in EMAIL_TRIGGERS and config.NOTIFY:
        notify.notify_upload_complete(sid)


def _after_agent_upload(sid):
    """A session an agent ran has finished. Unmounting is the agent's own
    business (its drive, its host); the report email is the master's."""
    s = db.get_session(sid)
    if s and s["trigger"] in EMAIL_TRIGGERS and s["status"] != "stopped" and config.NOTIFY:
        notify.notify_upload_complete(sid)


up.on_session_done = _after_upload


def _start_session(location, source, label, trigger, items=None, bucket=None, prefix=None):
    """Queue an upload here, or on agent `location` (which picks it up at its
    next check-in — woken right away)."""
    if not location:
        return up.enqueue(source, label, trigger, items=items, bucket=bucket, prefix=prefix)
    bucket = bucket or config.BUCKET
    awsapi.bucket_region(bucket)   # known before the agent's next check-in carries it
    label = label or os.path.basename(source.rstrip("/")) or "upload"
    sid = db.create_session(bucket, source, label, trigger, location=location, items=items,
                            prefix=prefix)
    what = f"{len(items)} selected item(s) under {source}" if items else source
    log_event("INFO", "upload", f"Session #{sid} queued on {location}: {what} -> "
                                f"s3://{bucket} (label='{label}', trigger={trigger})")
    hub.wake(location)
    return sid


tracker = drives.DriveTracker(
    lambda location, mount, label, bucket: _start_session(location, mount, label, "canary",
                                                          bucket=bucket))
watch = watcher_mod.Watcher(lambda found: tracker.update("", found))
hub = agents_mod.Hub(tracker, _after_agent_upload)

# Regions of the buckets met so far; new ones are looked up on first use.
buckets_mod.load_regions()

# A bucket picked in the UI is persisted in the DB and overrides the .env value
_saved_bucket = db.get_setting("bucket")
if _saved_bucket:
    config.BUCKET = _saved_bucket

_stale = db.fail_stale_uploads()
if _stale:
    log_event("WARNING", "app",
              f"marked {_stale} upload(s) interrupted by restart as failed — "
              f"re-run their session to retry")
_stale_sess = db.fail_stale_sessions()
if _stale_sess:
    log_event("WARNING", "app",
              f"marked {_stale_sess} upload session(s) left running by a restart as failed")
_stale_dl = db.fail_stale_downloads()
if _stale_dl:
    log_event("WARNING", "app",
              f"marked {_stale_dl} download(s) interrupted by restart as failed")


def _location(value):
    """'' for this master (also accepted: its own name), else a known agent."""
    value = (value or "").strip()
    if value in ("", config.NAME):
        return ""
    if not agents_mod.NAME_RE.match(value):
        raise localfs.FsError(f"unknown location '{value}'")
    return value


def _fs(location, op, **kwargs):
    """Run a filesystem operation where the files are: here, or on an agent."""
    if not location:
        return localfs.OPS[op](**kwargs)
    return hub.call(location, op, kwargs,
                    timeout=600 if op == "plan_upload"
                    else 180 if op in ("orphans", "delete_orphans", "verify_downloads") else 30)


@app.errorhandler(localfs.FsError)
@app.errorhandler(agents_mod.AgentError)
def _fs_error(e):
    return jsonify({"error": str(e)}), e.status


@app.get("/")
def index():
    return render_template("index.html", version=version.VERSION)


@app.get("/health")
def api_health():
    """Monitoring endpoint: upload/restore/download status + integrity signals.
    Reads only local DB + in-memory state (no AWS calls) unless ?check_aws=1.
    ?strict=1 returns HTTP 503 when status is 'attention'."""
    bucket = config.BUCKET
    fstats = db.stats(bucket).get("files", {})

    def fcount(s):
        return fstats.get(s, {}).get("count", 0)

    # --- uploads ---
    cur_sid = up.current_session
    active = None
    if cur_sid:
        s = db.get_session(cur_sid)
        if s:
            pct = (round(100 * s["done_bytes"] / s["total_bytes"]) if s["total_bytes"]
                   else (100 if s["status"] == "done" else 0))
            active = {
                "session_id": s["id"], "label": s["label"], "trigger": s["trigger"],
                "status": s["status"],
                "files_done": s["done_files"], "files_total": s["total_files"],
                "skipped": s["skipped_files"], "failed": s["failed_files"],
                "bytes_done": s["done_bytes"], "bytes_total": s["total_bytes"],
                "percent": pct, "started_at": s["started_at"],
            }
    last = db.latest_finished_session(bucket)
    last_session = None
    if last:
        last_session = {
            "session_id": last["id"], "label": last["label"], "trigger": last["trigger"],
            "status": last["status"], "files_total": last["total_files"],
            "uploaded": last["done_files"], "skipped": last["skipped_files"],
            "failed": last["failed_files"], "finished_at": last["finished_at"],
        }
    uploads = {
        "running": cur_sid is not None,
        "current_session": cur_sid,
        "queued": up.queue_size(),
        "active": active,
        "last_session": last_session,
    }

    # --- restores / downloads ---
    rsum = db.restore_summary()
    restores = {"in_progress": rsum["in_progress"], "completed": rsum["completed"],
                "failed": rsum["failed"], "last_checked": rsum["last_checked"]}
    downloads = {"running": down.current_session is not None,
                 "current_session": down.current_session, "queued": down.queue_size()}

    # --- integrity signals (active bucket) ---
    asum = db.audit_issue_counts(bucket)
    msum = db.manifest_summary(bucket)
    integrity = {
        "failed_uploads": fcount("failed"),
        "manifest_mismatch": msum.get("mismatch", 0),
        "audit_missing": asum.get("missing", 0),
        "audit_size_mismatch": asum.get("size_mismatch", 0),
        "audit_class_drift": asum.get("class_drift", 0),
    }

    body = {
        "status": "ok",
        "version": version.VERSION,
        "time": db.now(),
        "timezone": time.strftime("%Z (UTC%z)"),
        "uptime_seconds": int(time.time() - START_TIME),
        "bucket": bucket,
        "objects": {"verified": fcount("verified"), "remote": fcount("remote"),
                    "failed": fcount("failed")},
        "uploads": uploads,
        "restores": restores,
        "downloads": downloads,
        "integrity": integrity,
        "manifest_check": {"enabled": config.MANIFEST_CHECK,
                           "formats": list(config.MANIFEST_EXTS),
                           "xxhash": manifest.xxhash_available()},
    }

    if request.args.get("check_aws"):
        try:
            awsapi.aws("sts", "get-caller-identity", log=False)
            awsapi.s3api("head-bucket", "--bucket", bucket, log=False)
            body["aws"] = {"ok": True}
        except AwsError as e:
            body["aws"] = {"ok": False, "error": str(e)[:200]}

    body["backup"], backup_issue = backup_mod.health()

    # An agent that went quiet while it still owes us work needs looking at.
    body["agents"] = [{"name": a["name"], "online": a["online"], "last_seen": a["last_seen"],
                       "version": a["version"], "pending": a["work"]}
                      for a in hub.listing()]
    agent_issue = any(not a["online"] and (a["pending"]["uploads"] or a["pending"]["downloads"])
                      for a in body["agents"])

    issues = (integrity["failed_uploads"] + integrity["manifest_mismatch"]
              + integrity["audit_missing"] + integrity["audit_size_mismatch"]
              + integrity["audit_class_drift"] + restores["failed"]
              + (0 if body.get("aws", {}).get("ok", True) else 1)
              + (1 if backup_issue else 0) + (1 if agent_issue else 0))
    busy = uploads["running"] or uploads["queued"] > 0 or downloads["running"]
    body["status"] = "attention" if issues else ("busy" if busy else "ok")

    code = 503 if (request.args.get("strict") and body["status"] == "attention") else 200
    return jsonify(body), code


@app.get("/api/status")
def api_status():
    return jsonify({
        "version": version.VERSION,
        "bucket": config.BUCKET,
        # The active bucket's own region (asked from S3 once, then remembered);
        # default_region is AWS_DEFAULT_REGION, used for anything without a bucket.
        "region": awsapi.bucket_region(config.BUCKET) if config.BUCKET else None,
        "default_region": os.environ.get("AWS_DEFAULT_REGION", ""),
        "prefix": config.PREFIX,
        "storage_class": config.STORAGE_CLASS,
        "watch_dirs": config.WATCH_DIRS,
        "canary": config.CANARY_NAME,
        "canary_buckets": config.CANARY_BUCKETS,
        "auto_upload": drives.auto_upload(""),
        "notify_enabled": config.NOTIFY,
        "notify_ready": notify.config_problem() is None,
        "email_to": config.EMAIL_TO,
        "active_mounts": tracker.drives(""),
        "name": config.NAME,
        "agents_enabled": bool(config.AGENT_TOKEN),
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


@app.get("/api/buckets/known")
def api_buckets_known():
    """Upload targets worth offering without calling AWS: the active bucket, the
    configured default, the canary allowlist and every bucket already indexed.
    (/api/buckets lists the whole account, which costs a list-buckets call.)"""
    return jsonify({"buckets": buckets_mod.known(), "active": config.BUCKET,
                    "canary_buckets": buckets_mod.allowlist(),
                    "regions": buckets_mod.regions()})


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


@app.post("/api/index/clear")
def api_index_clear():
    """Wipe local index metadata for a bucket (or all). Only the local database
    is affected — S3 objects are never touched. Requires a typed confirmation
    matching the bucket name (or 'ALL') to prevent accidents."""
    data = request.get_json(force=True)
    bucket = (data.get("bucket") or "").strip()
    confirm = (data.get("confirm") or "").strip()
    everything = bucket in ("", "*", "ALL")
    expected = "ALL" if everything else bucket
    if confirm != expected:
        return jsonify({"error": f"confirmation must equal '{expected}'"}), 400
    counts = db.clear_index("*" if everything else bucket)
    total = sum(counts.values())
    log_event("WARNING", "app",
              f"local index cleared for {expected}: {total} row(s) removed {counts}")
    # If we wiped the active bucket's data, that's fine — the bucket setting stays.
    return jsonify({"ok": True, "cleared": expected, "counts": counts, "total": total})


@app.get("/api/browse/roots")
def api_browse_roots():
    """The upload roots of this machine or of agent ?location=…, each checked
    for whether it exists and is readable there."""
    return jsonify(_fs(_location(request.args.get("location")), "browse_roots"))


@app.get("/api/browse")
def api_browse():
    return jsonify(_fs(_location(request.args.get("location")), "browse",
                       path=request.args.get("path") or None))


def _key_parts(v):
    p = [x.strip() for x in str(v or "").replace("\\", "/").split("/")]
    if any(x in (".", "..") for x in p):
        raise ValueError("a prefix or label can't contain '.' or '..' as a folder name")
    return [x for x in p if x]


def _manual_prefix(data):
    """The typed prefix, tidied: "Film / RAW/" -> "Film/RAW". Raises ValueError."""
    return "/".join(_key_parts(data.get("prefix")))


def _manual_label(data, source):
    """Where a manual upload's keys start (after COLDVAULT_PREFIX): the optional
    prefix — e.g. the film — then the label, which is the folder's own name
    unless one is typed. "Film/Day01" and "Film" + "Day01" are the same."""
    label = _key_parts(data.get("label")) or [os.path.basename(source.rstrip("/")) or "upload"]
    return "/".join(_key_parts(data.get("prefix")) + label)


@app.get("/api/upload/prefixes")
def api_upload_prefixes():
    """Suggestions for the manual-upload prefix in one bucket: prefixes typed
    before, and the folders already at the top of the bucket."""
    bucket = (request.args.get("bucket") or "").strip() or config.BUCKET
    return jsonify({"bucket": bucket, "global_prefix": config.PREFIX or "",
                    "prefixes": db.used_prefixes(bucket),
                    "folders": db.top_folders(bucket, (config.PREFIX or "").strip("/"))})


def _upload_bucket(data):
    """The destination of a manual upload: explicit (the UI always sends it), or
    the active bucket when omitted. Raises BucketError."""
    requested = (data.get("bucket") or "").strip()
    bucket = buckets_mod.check(requested) if requested else config.BUCKET
    if not bucket:
        raise buckets_mod.BucketError("no bucket set — pick one in Connection, or set "
                                      "COLDVAULT_BUCKET")
    return bucket


@app.post("/api/upload/preview")
def api_upload_preview():
    """What Start upload would do with this selection: per file, uploads /
    skipped / checked during upload / refused, by the uploader's own rules."""
    data = request.get_json(force=True)
    location = _location(data.get("location"))
    try:
        bucket = _upload_bucket(data)
    except buckets_mod.BucketError as e:
        return jsonify({"error": str(e)}), 400
    plan = _fs(location, "plan_upload", path=data.get("path"), items=data.get("items"))
    try:
        label = _manual_label(data, plan["source"])
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(upload_plan.classify(bucket, label, location, plan))


@app.post("/api/upload/count")
def api_upload_count():
    """Files and bytes a manual upload would send (before skipping anything
    already archived), and where its keys start — for Start upload's
    confirmation. Counted on the machine that has the files."""
    data = request.get_json(force=True)
    location = _location(data.get("location"))
    try:
        bucket = _upload_bucket(data)
    except buckets_mod.BucketError as e:
        return jsonify({"error": str(e)}), 400
    plan = _fs(location, "plan_upload", path=data.get("path"), items=data.get("items"),
               summary=True)
    try:
        label = _manual_label(data, plan["source"])
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(dict(plan, bucket=bucket, label=label, location=location or config.NAME,
                        key_prefix=uploader_mod.make_key(label, "")))


@app.post("/api/upload")
def api_upload():
    data = request.get_json(force=True)
    location = _location(data.get("location"))
    try:
        bucket = _upload_bucket(data)
    except buckets_mod.BucketError as e:
        return jsonify({"error": str(e)}), 400

    # Checked (and a selection reduced to its common folder) on the machine
    # that has the files.
    plan = _fs(location, "prepare_upload", path=data.get("path"), items=data.get("items"))
    try:
        label = _manual_label(data, plan["source"])
        prefix = _manual_prefix(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    sid = _start_session(location, plan["source"], label, "manual",
                         items=plan["items"], bucket=bucket, prefix=prefix)
    return jsonify({"session_id": sid, "bucket": bucket, "location": location or config.NAME})


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
        manifest=request.args.get("manifest") or None,
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


@app.post("/api/manifest/verify")
def api_manifest_verify():
    """Verify offload manifest(s) against what's already archived.
    Input: one or more uploaded files (CSV/MHL) under 'file', OR a server
    'folder' to scan for manifests. Options: bucket ('*' = all), scope (key
    substring filter), deep=1 to re-hash still-mounted source files."""
    bucket = (request.form.get("bucket") or "").strip() or config.BUCKET
    if bucket == "*":
        bucket = None
    scope = (request.form.get("scope") or "").strip() or None
    deep = request.form.get("deep") in ("1", "true", "on")
    folder = (request.form.get("folder") or "").strip()

    records, sources = [], []
    if folder:
        if not localfs.allowed_path(folder) or not os.path.isdir(folder):
            return jsonify({"error": f"folder must be inside: {', '.join(config.BROWSE_ROOTS)}"}), 400
        records, found = manifest.load_manifest_records(folder, config.MANIFEST_EXTS)
        sources = [os.path.relpath(p, folder) for p in found]
    else:
        for up_file in request.files.getlist("file"):
            if not up_file or not up_file.filename:
                continue
            fd, tmp = tempfile.mkstemp(dir=config.TMP_DIR, suffix=".manifest")
            os.close(fd)
            try:
                up_file.save(tmp)
                recs, _fmt = manifest.parse_any(tmp, up_file.filename)
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            if recs:
                records.extend(recs)
                sources.append(up_file.filename)
    if not records:
        return jsonify({"error": "no file hashes found — is this a CSV/MHL checksum manifest?"}), 400

    rep = manifest_verify.verify(records, bucket, scope)
    deep_items = rep.pop("_deep")
    rep["sources"] = sources
    rep["xxhash"] = manifest.xxhash_available()
    if deep and deep_items:
        rep["job_id"] = manifest_verify.start_deep(deep_items, ", ".join(sources[:3]))
    return jsonify(rep)


@app.get("/api/manifest/jobs")
def api_manifest_jobs():
    return jsonify(manifest_verify.jobs())


@app.get("/api/eject/pending")
def api_eject_pending():
    """Drives whose canary upload finished cleanly and can now be unmounted.
    Polled by the host-side helper (the container cannot unmount host mounts)."""
    return jsonify(eject.listing())


@app.post("/api/eject/done")
def api_eject_done():
    """Host helper reports the outcome of an unmount."""
    data = request.get_json(force=True)
    remaining = eject.done((data.get("mount") or "").strip(), bool(data.get("ok")),
                           (data.get("detail") or "").strip())
    return jsonify({"ok": True, "remaining": remaining})


@app.get("/api/tree")
def api_tree():
    """Lazy tree of the archive: sub-folders (with subtree totals) and the files
    directly under a key prefix."""
    bucket = request.args.get("bucket") or config.BUCKET
    prefix = request.args.get("prefix") or ""
    folders, files = db.tree_children(bucket, prefix)
    return jsonify({
        "bucket": bucket, "prefix": prefix,
        "folders": [{"name": f["name"], "prefix": prefix + f["name"],
                     "count": f["c"], "bytes": f["b"]} for f in folders],
        "files": [{"key": f["key"], "name": f["key"][len(prefix):], "size": f["size"],
                   "status": f["status"], "manifest_state": f["manifest_state"],
                   "expires_at": f["expires_at"]} for f in files],
    })


@app.get("/api/tree/buckets")
def api_tree_buckets():
    """Roots of the Tree view: every bucket in the index, plus the active one
    even if nothing has been indexed in it yet."""
    rows = {r["bucket"]: {"bucket": r["bucket"], "count": r["c"], "bytes": r["b"]}
            for r in db.bucket_totals()}
    if config.BUCKET and config.BUCKET not in rows:
        rows[config.BUCKET] = {"bucket": config.BUCKET, "count": 0, "bytes": 0}
    return jsonify({"buckets": sorted(rows.values(), key=lambda r: r["bucket"]),
                    "active": config.BUCKET})


@app.get("/api/tree/export")
def api_tree_export():
    """Download the whole tree (folders with subtree sizes, plus every file and
    the totals) as Excel, PDF or CSV. Index-only, so no AWS calls."""
    bucket = request.args.get("bucket") or config.BUCKET
    prefix = request.args.get("prefix") or ""
    fmt = (request.args.get("format") or "xlsx").lower()
    try:
        blob, name, mime = treeexport.build(bucket, prefix, fmt)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:          # optional dependency missing
        log_event("ERROR", "app", f"tree export ({fmt}) unavailable: {e}")
        return jsonify({"error": str(e)}), 501
    log_event("INFO", "app", f"tree exported as {fmt}: s3://{bucket}"
                             + (f"/{prefix}" if prefix else "") + f" -> {name}")
    return Response(blob, mimetype=mime, headers={
        "Content-Disposition": f'attachment; filename="{name}"',
        "Content-Length": str(len(blob)),
    })


@app.get("/api/retention/permission")
def api_retention_permission():
    """Whether this IAM user can delete — probed once, then cached. ?recheck=1
    re-probes (the only thing that issues another delete-object)."""
    bucket = request.args.get("bucket") or config.BUCKET
    recheck = request.args.get("recheck") in ("1", "true", "yes")
    allowed, detail, checked_at = retention.can_delete(bucket, recheck=recheck)
    # can_delete is about the everyday user. With a delete role configured,
    # deleting is still possible — but only by unlocking the role each time.
    return jsonify({"bucket": bucket, "can_delete": allowed, "detail": detail,
                    "checked_at": checked_at, "probe_enabled": config.DELETE_PROBE,
                    "delete_role": retention.delete_identity() if config.DELETE_ROLE_ARN
                    else None})


@app.post("/api/retention/role-test")
def api_retention_role_test():
    """Unlock the delete role (with the MFA code) and check it may delete from
    the bucket — using the same harmless probe, nothing is removed."""
    data = request.get_json(force=True)
    bucket = (data.get("bucket") or "").strip() or config.BUCKET
    try:
        return jsonify(retention.test_role(bucket, data.get("mfa_code")))
    except retention.DeleteAuthError as e:
        return jsonify({"error": str(e)}), 403


@app.post("/api/retention/free-from")
def api_retention_free_from():
    """The earliest charge-free expiry date for a selection (no AWS call), and —
    given a proposed `date` — how many objects would still be inside their
    minimum storage duration on it."""
    data = request.get_json(force=True)
    bucket = (data.get("bucket") or "").strip() or config.BUCKET
    rows, err = _retention_rows(data, bucket)
    if err:
        return jsonify({"error": err}), 400
    as_of = None
    if data.get("date"):
        try:
            as_of = datetime.strptime(data["date"], "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    return jsonify({"bucket": bucket, "count": len(rows), **retention.free_from(rows, as_of)})


def _retention_rows(data, bucket):
    """Resolve a request body to index rows: explicit keys and/or key prefixes."""
    keys = data.get("keys") or None
    prefixes = data.get("prefixes") or None
    prefix = (data.get("prefix") or "").strip() or None
    if prefix:
        prefixes = (prefixes or []) + [prefix]
    if not keys and not prefixes:
        return None, "give keys[] and/or prefix(es)"
    return db.keys_under(bucket, prefixes, keys), None


@app.post("/api/retention/preview")
def api_retention_preview():
    """What would be affected: count, bytes, early-deletion exposure, versioning."""
    data = request.get_json(force=True)
    bucket = (data.get("bucket") or "").strip() or config.BUCKET
    rows, err = _retention_rows(data, bucket)
    if err:
        return jsonify({"error": err}), 400
    label = data.get("prefix") or ", ".join(data.get("prefixes") or []) or "selected keys"
    return jsonify(retention.preview_rows(bucket, rows, label))


@app.post("/api/retention/expiry")
def api_retention_expiry():
    """Set (or clear, with date=null) the planned deletion date. Never deletes."""
    data = request.get_json(force=True)
    bucket = (data.get("bucket") or "").strip() or config.BUCKET
    when = (data.get("date") or "").strip() or None
    if when:
        try:
            datetime.strptime(when, "%Y-%m-%d")
            when = f"{when} 00:00:00"
        except ValueError:
            return jsonify({"error": "date must be YYYY-MM-DD (or null to clear)"}), 400
    rows, err = _retention_rows(data, bucket)
    if err:
        return jsonify({"error": err}), 400
    n = db.set_expiry(bucket, [r["key"] for r in rows], when)
    log_event("INFO", "retention",
              f"expiry {'set to ' + when if when else 'cleared'} for {n} object(s) "
              f"in s3://{bucket}" + (f" under '{data.get('prefix')}'" if data.get("prefix") else ""))
    return jsonify({"updated": n, "expires_at": when})


@app.get("/api/retention/due")
def api_retention_due():
    # bucket: absent -> active bucket, "*" -> every bucket (an expiry set from
    # the Tree can be in any of them), else that bucket.
    bucket = request.args.get("bucket") or config.BUCKET
    one = None if bucket == "*" else bucket
    # scope=all lists everything scheduled, not just what's already due — so a
    # date set for next year is visible instead of looking like it didn't take.
    scope = (request.args.get("scope") or "due").lower()
    now = db.now()
    due_rows = db.files_due(one, now)
    rows = db.files_scheduled(one) if scope == "all" else due_rows
    sched_count, sched_bytes = db.count_scheduled(one)
    return jsonify({
        "bucket": bucket, "scope": scope,
        "due": len(due_rows),
        "due_bytes": sum(int(r["size"] or 0) for r in due_rows),
        "due_buckets": sorted({r["bucket"] for r in due_rows}),
        "scheduled": sched_count, "scheduled_bytes": sched_bytes,
        "next_expiry": db.next_expiry(one),
        "shown": len(rows[:500]), "truncated": len(rows) > 500,
        "items": [{"bucket": r["bucket"], "key": r["key"], "size": r["size"],
                   "expires_at": r["expires_at"],
                   "uploaded_at": r["uploaded_at"],
                   "early_days": retention.early_days_at_expiry(r),
                   "days_left": retention.days_left(r),
                   "due": (r["expires_at"] or "") <= now}
                  for r in rows[:500]],
    })


@app.post("/api/retention/delete")
def api_retention_delete():
    """PERMANENTLY delete objects from S3 and the index. Requires the bucket name
    as a typed confirmation. Writes a tombstone + log line for every object."""
    data = request.get_json(force=True)
    bucket = (data.get("bucket") or "").strip() or config.BUCKET

    def refuse(msg, status=400):
        # Every refused delete leaves a trace in the log, not just in the browser.
        log_event("WARNING", "retention", f"delete refused on s3://{bucket}: {msg}")
        return jsonify({"error": msg}), status

    if bucket == "*":
        return refuse("delete works on one bucket at a time")
    if (data.get("confirm") or "").strip() != bucket:
        return refuse(f"confirmation must equal the bucket name '{bucket}'")
    mode = "expiry" if data.get("due") else "manual"
    if data.get("due"):
        rows = db.files_due(bucket, db.now())
    else:
        rows, err = _retention_rows(data, bucket)
        if err:
            return refuse(err)
    if not rows:
        return refuse("nothing matched — refusing to delete")
    try:
        # With a delete role configured, this is the only moment it is unlocked.
        creds = retention.unlock(data.get("mfa_code"))
    except retention.DeleteAuthError as e:
        return jsonify({"error": str(e), "auth": True}), 403
    try:
        rep = retention.delete(bucket, rows, (data.get("reason") or "").strip(), mode, creds)
    except AwsError as e:
        return jsonify({"error": str(e)[:500]}), 502
    finally:
        creds = None
    return jsonify(rep)


@app.get("/api/deletions")
def api_deletions():
    bucket = request.args.get("bucket")
    if bucket == "*":
        bucket = None
    elif not bucket:
        bucket = config.BUCKET
    return jsonify({"items": db.list_deletions(bucket),
                    "totals": db.deletion_totals(bucket)})


@app.get("/api/backup")
def api_backup_status():
    return jsonify(backup_mod.status())


@app.post("/api/backup")
def api_backup_run():
    """Take a snapshot now and answer only when it's finished, so a host script
    can chain its own tool: `curl -fsS -X POST …/api/backup && restic backup …`.
    Fails (HTTP 500) when the snapshot failed, so `curl -f` stops the chain."""
    data = request.get_json(silent=True) or {}
    trigger = "manual" if data.get("trigger") == "manual" else "api"
    try:
        r = backup_mod.run(trigger)
    except backup_mod.BackupBusy as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    return jsonify(r), (200 if r["ok"] else 500)


@app.post("/api/backup/settings")
def api_backup_settings():
    data = request.get_json(force=True)
    try:
        saved = backup_mod.save_settings(data.get("at"), data.get("keep"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, **saved, "next_run": backup_mod.status()["next_run"]})


# ---- second machines (agents) ----

@app.get("/api/agents")
def api_agents():
    return jsonify({"agents": hub.listing(), "enabled": bool(config.AGENT_TOKEN),
                    "master": config.NAME})


@app.post("/api/sessions/<int:sid>/stop")
def api_session_stop(sid):
    """Stop an upload session — here or on an agent. Files in flight finish or,
    if large, are abandoned between parts; nothing new starts."""
    s = db.stop_session(sid)
    if not s:
        return jsonify({"error": f"session #{sid} isn't queued or running"}), 409
    where = s["location"] or config.NAME
    log_event("WARNING", "upload", f"Session #{sid} on {where}: STOP requested "
                                   f"({s['done_files']} uploaded, {s['skipped_files']} skipped so far)")
    if s["location"]:
        hub.wake(s["location"])
    return jsonify({"ok": True, "was": s["status"], "location": where})


@app.post("/api/auto-upload")
def api_auto_upload():
    """Switch auto-upload of canary drives on or off for one machine."""
    data = request.get_json(force=True) or {}
    location = _location(data.get("location"))
    drives.set_auto_upload(location, bool(data.get("enabled")))
    return jsonify({"location": location or config.NAME,
                    "enabled": drives.auto_upload(location)})


@app.post("/api/drives/upload")
def api_drive_upload_now():
    """Upload a plugged-in canary drive now (one that is waiting)."""
    data = request.get_json(force=True) or {}
    location = _location(data.get("location"))
    try:
        sid = tracker.upload_now(location, (data.get("mount") or "").strip())
    except LookupError as e:
        return jsonify({"error": str(e)}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    return jsonify({"session_id": sid})


@app.post("/api/agents/forget")
def api_agents_forget():
    name = ((request.get_json(force=True) or {}).get("name") or "").strip()
    n = hub.forget(name)
    log_event("INFO", "agent", f"forgot agent {name}")
    return jsonify({"ok": bool(n)})


@app.get("/api/locations")
def api_locations():
    """Machines that can hold files: this master, then each known agent."""
    return jsonify({"locations": [{"value": "", "name": config.NAME, "online": True,
                                   "local": True}] +
                    [{"value": a["name"], "name": a["name"], "online": a["online"],
                      "local": False} for a in hub.listing()]})


def _agent():
    """Authenticate an agent request; returns its name."""
    return hub.authenticate(request)


@app.post("/api/agent/poll")
def api_agent_poll():
    name = _agent()
    return jsonify(hub.poll(name, request.get_json(force=True) or {}))


@app.post("/api/agent/db")
def api_agent_db():
    name = _agent()
    return jsonify({"result": hub.db_call(name, request.get_json(force=True) or {})})


@app.post("/api/agent/reply")
def api_agent_reply():
    name = _agent()
    hub.reply(name, request.get_json(force=True) or {})
    return jsonify({"ok": True})


@app.post("/api/agent/events")
def api_agent_events():
    name = _agent()
    hub.events(name, (request.get_json(force=True) or {}).get("events"))
    return jsonify({"ok": True})


@app.get("/api/sessions")
def api_sessions():
    return jsonify(db.list_sessions())


@app.post("/api/restore")
def api_restore():
    data = request.get_json(force=True)
    items = data.get("items") or []
    # A ticked folder in the tree arrives as a prefix: expand it from the index.
    prefixes = data.get("prefixes") or []
    if prefixes:
        bucket = (data.get("bucket") or "").strip() or config.BUCKET
        seen = {(i.get("bucket") or bucket, i.get("key")) for i in items}
        for r in db.keys_under(bucket, prefixes, None):
            if (r["bucket"], r["key"]) not in seen:
                items.append({"bucket": r["bucket"], "key": r["key"]})
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
                               status="remote",
                               uploaded_at=db.to_local(obj.get("LastModified")))
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
    try:
        return jsonify(audit_mod.run_audit(config.BUCKET))
    except AwsError as e:
        return jsonify({"error": str(e)[:500]}), 502


@app.post("/api/notify/test")
def api_notify_test():
    problem = notify.config_problem()
    if problem:
        return jsonify({"ok": False, "error": problem}), 400
    ok, detail = notify.send_email(
        "ColdVault test email",
        "<div style='font-family:sans-serif'>✅ ColdVault email notifications are "
        "configured correctly.</div>")
    if ok:
        return jsonify({"ok": True, "id": detail})
    return jsonify({"ok": False, "error": detail}), 502


@app.post("/api/notify/report")
def api_notify_report():
    """Run an audit now and email the report on demand."""
    problem = notify.config_problem()
    if problem:
        return jsonify({"ok": False, "error": problem}), 400
    try:
        ok, detail = notify.send_audit_report()
    except AwsError as e:
        return jsonify({"ok": False, "error": str(e)[:500]}), 502
    if ok:
        return jsonify({"ok": True, "id": detail})
    return jsonify({"ok": False, "error": detail}), 502


@app.get("/api/restored")
def api_restored():
    """Objects whose latest restore completed — i.e. downloadable right now.
    Each item is tagged with its state on the chosen machine (?location=):
    present / deleted / none, and whether its restore window has expired."""
    location = _location(request.args.get("location"))
    items = db.restored_objects()
    dmap = db.latest_downloads_map(location)
    ok = ("verified", "downloaded", "skipped")
    claimed = [d["local_path"] for d in dmap.values() if d["status"] in ok]
    try:  # ask the machine that has the files which of them are still there
        exists = dict(zip(claimed, _fs(location, "paths_exist", paths=claimed)))
        checked = True
    except agents_mod.AgentError:
        exists, checked = {}, False   # agent offline: go by the last known state
    for i in items:
        d = dmap.get((i["bucket"], i["key"]))
        i["downloaded_to"] = None
        if d and d["status"] in ok:
            if not checked or exists.get(d["local_path"]):
                i["local_state"] = "present"
                i["downloaded_to"] = d["local_path"]
            else:  # was downloaded, file now gone (integrity check not yet run)
                i["local_state"] = "deleted"
                i["prev_path"] = d["local_path"]
        elif d and d["status"] == "deleted":
            i["local_state"] = "deleted"
            i["prev_path"] = d["local_path"]
        else:  # never downloaded, or a failed attempt -> available to (re)download
            i["local_state"] = "none"
        i["expired"] = downloader_mod.restore_expired(i.get("expiry"))
    download_dir = config.DOWNLOAD_DIR
    if location:
        agent = next((a for a in hub.listing() if a["name"] == location), None)
        download_dir = ((agent or {}).get("info") or {}).get("download_dir") or "/downloads"
    return jsonify({"items": items, "download_dir": download_dir,
                    "location": location or config.NAME, "checked": checked})


@app.post("/api/download/verify")
def api_download_verify():
    """Re-check that downloaded files still exist on the chosen machine; flag
    deleted ones."""
    data = request.get_json(silent=True) or {}
    return jsonify(_fs(_location(data.get("location")), "verify_downloads"))


@app.get("/api/download/orphans")
def api_download_orphans():
    """Files in the download folder that no index row points at, each checked
    against every indexed bucket so you can see what's still archived."""
    return jsonify(_fs(_location(request.args.get("location")), "orphans"))


@app.post("/api/download/orphans/delete")
def api_download_orphans_delete():
    """Delete local files from the download folder. S3 is never touched.

    Refuses a file that isn't archived in an indexed bucket — that copy may be
    the only one left — unless the caller explicitly passes force."""
    data = request.get_json(force=True)
    return jsonify(_fs(_location(data.get("location")), "delete_orphans",
                       paths=data.get("paths") or [], force=bool(data.get("force"))))


@app.get("/api/download/browse")
def api_download_browse():
    return jsonify(_fs(_location(request.args.get("location")), "download_browse",
                       path=request.args.get("path") or None))


@app.post("/api/download")
def api_download():
    """Queue a download to this machine or an agent (`location`). The space
    check runs on the machine that has the disk; if not everything fits, the
    UI is asked whether to fetch what fits (on_insufficient=fit) or cancel."""
    data = request.get_json(force=True)
    location = _location(data.get("location"))
    plan = _fs(location, "plan_download", dest=data.get("dest") or None,
               items=data.get("items") or [], policy=data.get("on_insufficient"))
    if not plan.get("go"):
        return jsonify(plan)
    sid = down.enqueue(plan["dest"], plan["items"], location=location)
    if location:
        hub.wake(location)
    body = {"session_id": sid}
    if plan.get("partial"):
        body.update({k: plan[k] for k in ("partial", "downloaded_count", "skipped_count",
                                          "skipped")})
    return jsonify(body)


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
    if config.AGENT_TOKEN and len(config.AGENT_TOKEN) < 20:
        log_event("WARNING", "agent", "COLDVAULT_AGENT_TOKEN is short — use a long random "
                                      "value, e.g. `openssl rand -hex 32`")
    watch.start()
    restore.start_poller()
    backup_mod.start()
    log_event("INFO", "app",
              f"ColdVault {version.VERSION} started — bucket={config.BUCKET or '(not set!)'}, "
              f"storage_class={config.STORAGE_CLASS}, port={config.PORT}")
    log_event("INFO", "app",
              "local time is " + time.strftime("%Y-%m-%d %H:%M:%S %Z (UTC%z)") +
              " — if that isn't your wall clock, set TZ in .env (e.g. TZ=Europe/Prague) "
              "and recreate the container")
    if config.MANIFEST_CHECK:
        if manifest.xxhash_available():
            log_event("INFO", "manifest",
                      f"manifest check enabled ({', '.join(config.MANIFEST_EXTS)}), xxhash available")
        else:
            log_event("ERROR", "manifest",
                      "manifest check enabled but the xxhash library is NOT installed — "
                      "XXH64/XXH3 manifests cannot be verified. Rebuild the image: "
                      "docker compose build --no-cache coldvault && docker compose up -d")
    app.run(host="0.0.0.0", port=config.PORT, threaded=True)
