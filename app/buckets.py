"""Which bucket an upload goes to.

ColdVault has one *active* bucket (the .env value, overridden by the picker in
the UI), but every upload session is pinned to its own bucket when it is queued.
That is what lets a canary file send one drive somewhere else and a manual
upload be aimed by hand. This module is the only place that decides whether a
requested bucket may be used, so the watcher and the API can't disagree.
"""

import time

import awsapi
import config
import db
from awsapi import AwsError
from logs import log_event


class BucketError(ValueError):
    """A requested bucket can't be used; the message is meant for the user."""


def allowlist():
    """Buckets a canary is allowed to name. Empty = no restriction."""
    return list(config.CANARY_BUCKETS)


def known():
    """Buckets worth offering as upload targets without calling AWS: the active
    one, the configured default, the canary allowlist, and anything already in
    the index. Order preserved, deduplicated."""
    out = []
    for b in ([config.BUCKET, config.ENV_BUCKET] + allowlist()
              + db.distinct_buckets()):
        b = (b or "").strip()
        if b and b not in out:
            out.append(b)
    return out


_failed = {}   # bucket -> when its region lookup last failed (retried after a while)


def lookup_region(name):
    """Ask S3 which region a bucket is in — once: the answer is kept in the
    database and in awsapi.REGIONS (and sent to agents with their policy).
    Master only; set as awsapi.lookup."""
    if time.time() - _failed.get(name, 0) < 600:
        return None
    try:
        r = awsapi.s3api("head-bucket", "--bucket", name, log=False, log_errors=False)
        region = (r or {}).get("BucketRegion")
    except AwsError:
        region = None
    if not region or not awsapi.REGION_RE.match(region):
        _failed[name] = time.time()
        return None
    awsapi.set_regions({name: region})
    db.set_setting(f"bucket_region:{name}", region)
    log_event("INFO", "app", f"bucket '{name}' is in {region} — its requests go straight there")
    return region


def load_regions():
    """Master start-up: the regions already known."""
    awsapi.set_regions({k.split(":", 1)[1]: v for k, v in db.settings_like("bucket_region:")})
    awsapi.lookup = lookup_region


def regions():
    return dict(awsapi.REGIONS)


def check(name, source="manual"):
    """Validate a bucket name as an upload target; return it, or raise
    BucketError. `source="canary"` marks a name read off removable media, which
    is additionally held to COLDVAULT_CANARY_BUCKETS when that list is set."""
    name = (name or "").strip()
    if not name:
        raise BucketError("no bucket given")
    # Cheap sanity check before spending a round trip on an obvious typo.
    if "/" in name or not (3 <= len(name) <= 63):
        raise BucketError(f"'{name}' is not a valid bucket name")
    if source == "canary" and config.CANARY_BUCKETS and name not in config.CANARY_BUCKETS:
        raise BucketError(
            f"canary names bucket '{name}', which is not in COLDVAULT_CANARY_BUCKETS "
            f"({', '.join(config.CANARY_BUCKETS)})")
    try:
        awsapi.s3api("head-bucket", "--bucket", name, log=False)
    except AwsError as e:
        raise BucketError(f"bucket '{name}' is not accessible: {str(e)[:200]}")
    return name
