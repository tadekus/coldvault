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
    ignore = tuple(config.AUDIT_IGNORE)
    # Anything archived after the listing starts can't be in it: not "missing".
    started = db.now()
    remote = {}
    total_bytes = 0
    ignored, ignored_bytes = 0, 0
    for obj in awsapi.list_all_objects(bucket, config.PREFIX):
        if obj["Key"].endswith("/"):
            continue
        remote[obj["Key"]] = obj
        total_bytes += int(obj.get("Size") or 0)

    with _lock:
        dropped = db.drop_remote_under(bucket, ignore) if ignore else 0
        db.clear_audit(bucket)
        when = db.now()
        indexed = db.files_for_audit(bucket)
        indexed_keys = {r["key"] for r in indexed}
        # Not archive content (e.g. access logs) and not uploaded by ColdVault:
        # leave it out of everything below.
        for key in [k for k in remote if k.startswith(ignore) and k not in indexed_keys] \
                if ignore else []:
            ignored += 1
            ignored_bytes += int(remote.pop(key).get("Size") or 0)
        total_bytes -= ignored_bytes
        missing, size_mismatch, class_drift, ok, newer = [], [], [], 0, 0
        updates = []

        for r in indexed:
            obj = remote.get(r["key"])
            if obj is None:
                if (r["verified_at"] or r["uploaded_at"] or "") >= started:
                    newer += 1   # archived while the bucket was being listed
                    continue
                updates.append(("missing", when, r["id"]))
                missing.append(r["key"])
            elif r["size"] is not None and obj.get("Size") is not None \
                    and int(r["size"]) != int(obj["Size"]):
                updates.append(("size_mismatch", when, r["id"]))
                size_mismatch.append({"key": r["key"], "index": r["size"], "bucket": obj["Size"]})
            elif obj.get("StorageClass") and obj["StorageClass"] != config.STORAGE_CLASS:
                updates.append(("class_drift", when, r["id"]))
                class_drift.append({"key": r["key"], "class": obj["StorageClass"]})
            else:
                updates.append(("ok", when, r["id"]))
                ok += 1
        db.set_audit_many(updates)

        imported = 0
        for key, obj in remote.items():
            if key not in indexed_keys and not db.get_file(bucket, key):
                db.upsert_file(bucket, key, size=obj.get("Size"),
                               etag=(obj.get("ETag") or "").strip('"'),
                               storage_class=obj.get("StorageClass"),
                               status="remote",
                               uploaded_at=db.to_local(obj.get("LastModified")),
                               audit_state="ok", audited_at=when)
                imported += 1

    # Re-surface the offload-manifest verification recorded at upload time, so a
    # mismatch is flagged by every audit, not only when it was first uploaded.
    msummary = db.manifest_summary(bucket)
    m_ok = msummary.get("ok", 0)
    m_mismatch = msummary.get("mismatch", 0)
    m_not_listed = msummary.get("not_in_manifest", 0)
    m_accepted = msummary.get("accepted", 0)   # reviewed mismatches: not an issue
    m_keys = db.manifest_mismatch_keys(bucket)

    level = "ERROR" if (missing or m_mismatch) else ("WARNING" if (size_mismatch or class_drift) else "INFO")
    log_event(level, "audit",
              f"bucket audit of s3://{bucket}: {ok} ok, {len(missing)} MISSING, "
              f"{len(size_mismatch)} size mismatch, {len(class_drift)} class drift, "
              f"{m_mismatch} manifest mismatch ({m_ok} manifest-verified, "
              f"{m_accepted} accepted mismatch), "
              f"{imported} imported ({len(remote)} objects in bucket"
              + (f", {ignored} ignored under {', '.join(ignore)}" if ignored else "")
              + (f", {newer} archived during the audit" if newer else "")
              + (f"; {dropped} previously imported ignored object(s) removed from the index"
                 if dropped else "") + ")")
    for k in missing:
        log_event("ERROR", "audit", f"MISSING from bucket (index says archived): {k}")
    for m in size_mismatch:
        log_event("WARNING", "audit",
                  f"size mismatch: {m['key']} index={m['index']} bucket={m['bucket']}")
    for k in m_keys:
        log_event("ERROR", "audit", f"manifest mismatch (offload hash != archived): {k}")

    problems = len(missing) + len(size_mismatch) + len(class_drift) + m_mismatch
    return {
        "bucket": bucket, "ok_status": problems == 0,
        "in_bucket": len(remote), "in_index": len(indexed), "bucket_bytes": total_bytes,
        "ok": ok, "imported": imported,
        "missing": missing[:200], "missing_count": len(missing),
        "size_mismatch": size_mismatch[:200], "size_mismatch_count": len(size_mismatch),
        "class_drift": class_drift[:200], "class_drift_count": len(class_drift),
        "manifest_ok": m_ok, "manifest_mismatch_count": m_mismatch,
        "manifest_not_listed": m_not_listed, "manifest_mismatch": m_keys[:200],
        "manifest_accepted": m_accepted,
        "ignored": ignored, "ignored_bytes": ignored_bytes, "ignored_prefixes": list(ignore),
        "newer": newer,
    }
