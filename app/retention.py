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
import math
import os
import re
import tempfile
from datetime import datetime, timedelta
from uuid import uuid4

import awsapi
import config
import db
from awsapi import AwsError
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


def early_days_at_expiry(row):
    """Days still owed under the minimum if the object is deleted on its planned
    expiry date — or now, once that date has passed."""
    now = datetime.now()
    return early_days(row, as_of=max(_parse(row.get("expires_at")) or now, now))


def free_from(rows, as_of=None):
    """When deleting these objects stops costing extra. Each object is free of
    the minimum-duration charge once its storage class's minimum (Deep Archive:
    180 days) has passed since upload; for a set, that's the LATEST of those.

    With `as_of` (a datetime — a proposed expiry date), also says how many
    would still be inside their minimum on that day, and by how much.

    Returns {"free_from": "YYYY-MM-DD" | None (nothing with a minimum),
    "newest_upload", "min_days", "unknown" (no upload date), "already_free",
    "early_at", "max_early_days_at"}."""
    ends, unknown, mins = [], 0, set()
    for r in rows:
        mind = min_days_for(r.get("storage_class"))
        if not mind:
            continue
        up = _parse(r.get("uploaded_at"))
        if not up:
            unknown += 1
            continue
        mins.add(mind)
        ends.append((up + timedelta(days=mind), up))
    out = {"free_from": None, "newest_upload": None, "unknown": unknown,
           "min_days": max(mins) if mins else min_days_for(config.STORAGE_CLASS),
           "already_free": True, "early_at": 0, "max_early_days_at": 0}
    if ends:
        end, newest = max(ends)
        # An expiry date means that day at 00:00, so a minimum that ends during
        # a day is only fully served from the next one.
        day = end.date() if end.time() == datetime.min.time() else (end + timedelta(days=1)).date()
        out.update(free_from=day.isoformat(), newest_upload=newest.strftime(db.TIME_FMT),
                   already_free=end <= datetime.now())
    if as_of:
        short = [(e - as_of).total_seconds() / 86400 for e, _ in ends if e > as_of]
        out.update(early_at=len(short),
                   max_early_days_at=math.ceil(max(short)) if short else 0)
    return out


def days_left(row, as_of=None):
    """Days until the planned deletion date; 0 once it's due, None if unset."""
    when = _parse(row.get("expires_at"))
    if not when:
        return None
    return max(0, (when - (as_of or datetime.now())).days)


def _probe_key(bucket):
    return f"delete_probe:{bucket}"


def can_delete(bucket, recheck=False):
    """Is this IAM user allowed to delete from the bucket?

    There is no read-only way to ask S3 this, so we probe: delete-object on a
    random key under a reserved prefix. Nothing of yours is ever named — the key
    doesn't exist, and S3's delete is idempotent, so deleting it removes nothing.

    The answer is **cached in the settings table** and only re-probed when asked
    (the Re-check button). An IAM policy doesn't change between page loads, and
    probing on every Retention/Tree render filled the log with alarming
    AccessDenied lines and — on a versioned bucket, where deleting a missing key
    writes a delete marker — would have littered the bucket.

    Returns (True|False|None, detail, checked_at); None means we couldn't tell."""
    skey = _probe_key(bucket)
    if not recheck:
        cached = db.get_setting(skey)
        if cached:
            try:
                c = json.loads(cached)
                return c.get("can_delete"), c.get("detail"), c.get("checked_at")
            except ValueError:
                pass
    if not config.DELETE_PROBE:
        return None, "probe disabled (COLDVAULT_DELETE_PROBE=false)", None

    key = f".coldvault-permission-probe/{uuid4().hex}"
    try:
        awsapi.s3api("delete-object", "--bucket", bucket, "--key", key,
                     log=False, log_errors=False)
        allowed, detail = True, None
    except Exception as e:
        msg = str(e)
        if "AccessDenied" in msg or "not authorized" in msg or "Forbidden" in msg:
            allowed, detail = False, "AccessDenied — the IAM user has no s3:DeleteObject"
        else:
            allowed, detail = None, msg[:200]
    checked_at = db.now()
    log_event("INFO", "retention",
              f"delete-permission probe on s3://{bucket}: "
              + ("ALLOWED — this IAM user can delete" if allowed is True else
                 "denied — append-only, nothing here can delete" if allowed is False
                 else f"inconclusive ({detail})")
              + " (probe targets a random non-existent key; nothing of yours is touched)")
    if allowed is not None:       # don't cache a network blip
        db.set_setting(skey, json.dumps(
            {"can_delete": allowed, "detail": detail, "checked_at": checked_at}))
    return allowed, detail, checked_at


class DeleteAuthError(Exception):
    """The delete role couldn't be unlocked — wrong or missing code, trust
    policy, … The message is for the user."""


def delete_identity():
    """How deletions are authorised here: through the delete role (with or
    without MFA), or with the everyday user."""
    return {"role": config.DELETE_ROLE_ARN or None,
            "mfa": bool(config.DELETE_ROLE_ARN and config.DELETE_MFA_SERIAL),
            "seconds": config.DELETE_SESSION_SECONDS}


def unlock(mfa_code=None, purpose="delete"):
    """Temporary credentials for the delete role, for one deletion. None when no
    role is configured (deletes then run as the everyday user, if it may).

    The role exists so the everyday user can't delete at all: AWS lets it only
    *assume* the role — and with an MFA-required trust policy, only with a
    fresh code from your authenticator. The credentials live in memory for this
    one request and expire on their own after a few minutes."""
    if not config.DELETE_ROLE_ARN:
        return None
    args = ["sts", "assume-role", "--role-arn", config.DELETE_ROLE_ARN,
            "--role-session-name", f"coldvault-{purpose}-{config.NAME}"[:64],
            "--duration-seconds", str(config.DELETE_SESSION_SECONDS)]
    if config.DELETE_MFA_SERIAL:
        code = (mfa_code or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            raise DeleteAuthError("enter the 6-digit code from your authenticator app")
        args += ["--serial-number", config.DELETE_MFA_SERIAL, "--token-code", code]
    try:
        # log=False: the command line carries the one-time code.
        r = awsapi.aws(*args, log=False, log_errors=False)
    except AwsError as e:
        msg = str(e)
        hint = (" — wrong or already-used code?" if config.DELETE_MFA_SERIAL and
                ("AccessDenied" in msg or "MultiFactor" in msg) else "")
        log_event("WARNING", "retention", f"delete role NOT unlocked ({purpose}): "
                                          f"{msg.strip().splitlines()[-1][:200]}")
        raise DeleteAuthError(f"couldn't unlock the delete role{hint} "
                              f"({msg.strip().splitlines()[-1][:200]})")
    c = (r or {}).get("Credentials") or {}
    if not c.get("AccessKeyId"):
        raise DeleteAuthError("the delete role returned no credentials")
    log_event("WARNING", "retention",
              f"delete role unlocked for {purpose}"
              + (" (MFA verified)" if config.DELETE_MFA_SERIAL else "")
              + f" — temporary credentials expire {db.to_local(c.get('Expiration'))}")
    return {"AWS_ACCESS_KEY_ID": c["AccessKeyId"], "AWS_SECRET_ACCESS_KEY": c["SecretAccessKey"],
            "AWS_SESSION_TOKEN": c["SessionToken"]}


def test_role(bucket, mfa_code=None):
    """Prove the delete setup end to end: unlock the role, then run the same
    harmless probe as can_delete() with its credentials (a delete of a random
    key that never existed — nothing is removed)."""
    creds = unlock(mfa_code, purpose="test")
    if creds is None:
        raise DeleteAuthError("no delete role configured (COLDVAULT_DELETE_ROLE_ARN)")
    try:
        awsapi.s3api("delete-object", "--bucket", bucket,
                     "--key", f".coldvault-permission-probe/{uuid4().hex}",
                     log=False, log_errors=False, creds=creds)
        ok, detail = True, "the role can delete from this bucket"
    except AwsError as e:
        msg = str(e)
        ok = False
        detail = ("the role was unlocked but has no s3:DeleteObject on this bucket"
                  if "AccessDenied" in msg else msg.strip().splitlines()[-1][:200])
    log_event("INFO", "retention", f"delete role test on s3://{bucket}: "
                                   f"{'OK' if ok else 'FAILED'} — {detail}")
    return {"ok": ok, "detail": detail}


def bucket_versioning(bucket):
    """'Enabled' / 'Suspended' / None. On a versioned bucket a delete only adds a
    delete marker — the data (and its cost) stays until the version is purged."""
    try:
        return (awsapi.s3api("get-bucket-versioning", "--bucket", bucket,
                             log=False, log_errors=False) or {}).get("Status")
    except Exception:
        # Usually just a missing s3:GetBucketVersioning — not knowing is fine,
        # the preview simply can't warn about delete markers. Not an error.
        return None


def preview(bucket, prefix=None, keys=None):
    return preview_rows(bucket, db.files_for_retention(bucket, prefix, keys), prefix)


def preview_rows(bucket, rows, prefix=None):
    total = sum(int(r["size"] or 0) for r in rows)
    early = [r for r in rows if early_days(r) > 0]
    early_bytes = sum(int(r["size"] or 0) for r in early)
    worst = max((early_days(r) for r in early), default=0)
    return {
        "bucket": bucket, "prefix": prefix, "count": len(rows), "bytes": total,
        "early_count": len(early), "early_bytes": early_bytes, "max_early_days": worst,
        "min_days": min_days_for(config.STORAGE_CLASS),
        "free_from": free_from(rows),
        "versioning": bucket_versioning(bucket),
        "sample": [{"key": r["key"], "size": r["size"], "uploaded_at": r["uploaded_at"],
                    "expires_at": r["expires_at"], "early_days": early_days(r)}
                   for r in rows[:200]],
    }


def _delete_batch(bucket, keys, creds=None):
    """Delete up to BATCH keys. Returns (deleted_keys, {key: error})."""
    payload = {"Objects": [{"Key": k} for k in keys], "Quiet": False}
    fd, path = tempfile.mkstemp(dir=config.TMP_DIR, suffix=".del.json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        resp = awsapi.s3api("delete-objects", "--bucket", bucket,
                            "--delete", f"file://{path}", creds=creds)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    done = [d.get("Key") for d in (resp.get("Deleted") or []) if d.get("Key")]
    errs = {e.get("Key"): f"{e.get('Code')}: {e.get('Message')}"
            for e in (resp.get("Errors") or [])}
    return done, errs


def delete(bucket, rows, reason, mode, creds=None):
    """Permanently delete the given index rows from S3 and the index, writing a
    tombstone for each. Returns a report. Never called automatically.
    `creds` are the delete role's (see unlock()); None = the everyday user."""
    report = {"requested": len(rows), "deleted": 0, "bytes": 0, "failed": 0,
              "early": 0, "errors": [], "versioning": bucket_versioning(bucket)}
    by_key = {r["key"]: r for r in rows}
    keys = list(by_key)
    log_event("WARNING", "retention",
              f"DELETE starting: {len(keys)} object(s) from s3://{bucket} "
              f"(mode={mode}, reason={reason or 'n/a'}"
              + (", as the delete role" if creds else "") + ")")

    for i in range(0, len(keys), BATCH):
        chunk = keys[i:i + BATCH]
        try:
            done, errs = _delete_batch(bucket, chunk, creds)
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
