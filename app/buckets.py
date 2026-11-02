"""Which bucket an upload goes to.

ColdVault has one *active* bucket (the .env value, overridden by the picker in
the UI), but every upload session is pinned to its own bucket when it is queued.
That is what lets a canary file send one drive somewhere else and a manual
upload be aimed by hand. This module is the only place that decides whether a
requested bucket may be used, so the watcher and the API can't disagree.
"""

import awsapi
import config
import db
from awsapi import AwsError


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
