import json
import re
import threading
import time

import awsapi
import config
import db
from awsapi import AwsError
from logs import log_event

VALID_TIERS = ("Standard", "Bulk")  # Expedited is not supported for Deep Archive


def request_restore(items, tier, days):
    """items: list of {"bucket": ..., "key": ...} — each restore targets the
    bucket the object actually lives in."""
    if tier not in VALID_TIERS:
        raise ValueError(f"tier must be one of {VALID_TIERS}")
    days = max(1, min(int(days), 365))
    results = []
    payload = json.dumps({"Days": days, "GlacierJobParameters": {"Tier": tier}})
    for item in items:
        bucket = (item.get("bucket") or config.BUCKET).strip()
        key = (item.get("key") or "").strip()
        if not key:
            continue
        try:
            awsapi.s3api("restore-object", "--bucket", bucket,
                         "--key", key, "--restore-request", payload)
            db.add_restore(bucket, key, tier, days, "in_progress")
            log_event("INFO", "restore",
                      f"restore requested ({tier}, {days}d): s3://{bucket}/{key}")
            results.append({"key": key, "ok": True})
        except AwsError as e:
            msg = str(e)
            if "RestoreAlreadyInProgress" in msg:
                db.add_restore(bucket, key, tier, days, "in_progress",
                               error="already in progress")
                log_event("WARNING", "restore",
                          f"restore already in progress: s3://{bucket}/{key}")
                results.append({"key": key, "ok": True, "note": "already in progress"})
            else:
                db.add_restore(bucket, key, tier, days, "failed", error=msg[:500])
                log_event("ERROR", "restore",
                          f"restore request failed: s3://{bucket}/{key}", msg[:1000])
                results.append({"key": key, "ok": False, "error": msg[:300]})
    return results


def check_pending():
    """head-object every in-progress restore and update its status."""
    pending = db.pending_restores()
    if not pending:
        return 0
    updated = 0
    for r in pending:
        try:
            resp = awsapi.s3api("head-object", "--bucket", r["bucket"] or config.BUCKET,
                                "--key", r["key"], log=False)
        except AwsError as e:
            db.update_restore(r["id"], last_checked=db.now(), error=str(e)[:500])
            continue
        header = resp.get("Restore") or ""
        if 'ongoing-request="false"' in header:
            m = re.search(r'expiry-date="([^"]+)"', header)
            expiry = m.group(1) if m else None
            db.update_restore(r["id"], status="completed", expiry=expiry,
                              last_checked=db.now())
            log_event("INFO", "restore",
                      f"restore COMPLETED: {r['key']}"
                      + (f" (available until {expiry})" if expiry else ""))
            updated += 1
        else:
            db.update_restore(r["id"], last_checked=db.now())
    return updated


def start_poller():
    def _loop():
        while True:
            time.sleep(config.RESTORE_POLL)
            try:
                check_pending()
            except Exception as e:
                log_event("ERROR", "restore", f"poller error: {e}")
    threading.Thread(target=_loop, daemon=True, name="restore-poller").start()
