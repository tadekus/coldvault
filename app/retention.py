"""Retention: schedule expiry for archived objects and delete them permanently.

Deleting is the only irreversible action in ColdVault, so everything here is
explicit and auditable:
  * nothing is ever deleted automatically — expiry only *flags* what is due;
  * a preview always precedes a delete (what, how much, what it will cost);
  * Deep Archive / Glacier bill a MINIMUM storage duration (180 / 90 days), so
    deleting early still charges the remaining days — the preview says so;
  * every deleted object leaves a tombstone row in `deletions` that outlives the
    index entry, plus an ERROR-free but explicit log line under 'retention'.
"""
import json
import os
import tempfile
from datetime import datetime, timedelta

import awsapi
import config
import db
from logs import log_event

# AWS minimum billable storage duration per storage class (days).
MIN_DAYS = {"DEEP_ARCHIVE": 180, "GLACIER": 90, "GLACIER_IR": 90,
            "STANDARD_IA": 30, "ONEZONE_IA": 30}
BATCH = 1000  # delete-objects accepts up to 1000 keys per call


def min_days_for(storage_class):
    return MIN_DAYS.get((storage_class or config.STORAGE_CLASS or "").upper(), 0)


def _parse(ts):
    if not ts:
        return None
    try:
        return datetime.strptime(str(ts)[:19], db.TIME_FMT)
    except ValueError:
        try:
            return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None


def early_days(row, as_of=None):
    """Days still owed under the storage-class minimum if deleted now (0 = none)."""
    up = _parse(row.get("uploaded_at"))
    mind = min_days_for(row.get("storage_class"))
    if not up or not mind:
        return 0
    free_on = up + timedelta(days=mind)
    rem = (free_on - (as_of or datetime.now())).days
    return max(0, rem)


def bucket_versioning(bucket):
    """'Enabled' / 'Suspended' / None. On a versioned bucket a delete only adds a
    delete marker — the data (and its cost) stays until the version is purged."""
    try:
        return (awsapi.s3api("get-bucket-versioning", "--bucket", bucket,
                             log=False) or {}).get("Status")
    except Exception:
        return None


def preview(bucket, prefix=None, keys=None):
    rows = db.files_for_retention(bucket, prefix, keys)
    total = sum(int(r["size"] or 0) for r in rows)
    early = [r for r in rows if early_days(r) > 0]
    early_bytes = sum(int(r["size"] or 0) for r in early)
    worst = max((early_days(r) for r in early), default=0)
    return {
        "bucket": bucket, "prefix": prefix, "count": len(rows), "bytes": total,
        "early_count": len(early), "early_bytes": early_bytes, "max_early_days": worst,
        "min_days": min_days_for(config.STORAGE_CLASS),
        "versioning": bucket_versioning(bucket),
        "sample": [{"key": r["key"], "size": r["size"], "uploaded_at": r["uploaded_at"],
                    "expires_at": r["expires_at"], "early_days": early_days(r)}
                   for r in rows[:200]],
    }


def _delete_batch(bucket, keys):
    """Delete up to BATCH keys. Returns (deleted_keys, {key: error})."""
    payload = {"Objects": [{"Key": k} for k in keys], "Quiet": False}
    fd, path = tempfile.mkstemp(dir=config.TMP_DIR, suffix=".del.json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        resp = awsapi.s3api("delete-objects", "--bucket", bucket,
                            "--delete", f"file://{path}")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    done = [d.get("Key") for d in (resp.get("Deleted") or []) if d.get("Key")]
    errs = {e.get("Key"): f"{e.get('Code')}: {e.get('Message')}"
            for e in (resp.get("Errors") or [])}
    return done, errs


def delete(bucket, rows, reason, mode):
    """Permanently delete the given index rows from S3 and the index, writing a
    tombstone for each. Returns a report. Never called automatically."""
    report = {"requested": len(rows), "deleted": 0, "bytes": 0, "failed": 0,
              "early": 0, "errors": [], "versioning": bucket_versioning(bucket)}
    by_key = {r["key"]: r for r in rows}
    keys = list(by_key)
    log_event("WARNING", "retention",
              f"DELETE starting: {len(keys)} object(s) from s3://{bucket} "
              f"(mode={mode}, reason={reason or 'n/a'})")

    for i in range(0, len(keys), BATCH):
        chunk = keys[i:i + BATCH]
        try:
            done, errs = _delete_batch(bucket, chunk)
        except Exception as e:
            report["failed"] += len(chunk)
            report["errors"].append(str(e)[:300])
            log_event("ERROR", "retention", f"delete batch failed: {e}")
            continue
        for k in done:
            row = by_key[k]
            ed = early_days(row)
            db.record_deletion(row, reason, mode, ed)
            db.drop_file(bucket, k)
            report["deleted"] += 1
            report["bytes"] += int(row["size"] or 0)
            report["early"] += 1 if ed else 0
            log_event("WARNING", "retention",
                      f"DELETED s3://{bucket}/{k} ({int(row['size'] or 0):,} bytes)"
                      + (f" — {ed} day(s) short of the {min_days_for(row.get('storage_class'))}-day "
                         f"minimum, still billed" if ed else ""))
        for k, err in errs.items():
            report["failed"] += 1
            report["errors"].append(f"{k}: {err}")
            log_event("ERROR", "retention", f"delete FAILED {k}: {err}")

    lvl = "ERROR" if report["failed"] else "WARNING"
    log_event(lvl, "retention",
              f"DELETE finished: {report['deleted']} removed "
              f"({report['bytes']:,} bytes), {report['failed']} failed, "
              f"{report['early']} before the minimum storage duration")
    if report["versioning"] == "Enabled" and report["deleted"]:
        log_event("WARNING", "retention",
                  "bucket versioning is ENABLED — deletes added delete markers; the "
                  "object versions (and their storage cost) remain until purged")
    return report
