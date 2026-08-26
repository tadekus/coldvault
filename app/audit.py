"""Reconcile the local index against the actual bucket contents. Shared by the
/api/audit endpoint and the post-upload email hook."""
import threading

import awsapi
import config
import db
from logs import log_event

_lock = threading.Lock()  # serialize audits (they clear/set audit_state)


def run_audit(bucket=None):
    """List the whole bucket (paginated) and compare with the index. Flags
    missing / size-mismatch / class-drift objects, imports bucket-only objects,
    and returns a report dict. Raises awsapi.AwsError on listing failure."""
    bucket = bucket or config.BUCKET
    remote = {}
    total_bytes = 0
    for obj in awsapi.list_all_objects(bucket, config.PREFIX):
        if obj["Key"].endswith("/"):
            continue
        remote[obj["Key"]] = obj
        total_bytes += int(obj.get("Size") or 0)

    with _lock:
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

    problems = len(missing) + len(size_mismatch) + len(class_drift)
    return {
        "bucket": bucket, "ok_status": problems == 0,
        "in_bucket": len(remote), "in_index": len(indexed), "bucket_bytes": total_bytes,
        "ok": ok, "imported": imported,
        "missing": missing[:200], "missing_count": len(missing),
        "size_mismatch": size_mismatch[:200], "size_mismatch_count": len(size_mismatch),
        "class_drift": class_drift[:200], "class_drift_count": len(class_drift),
    }
