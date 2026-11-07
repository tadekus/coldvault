import json
import os
import re
import shlex
import subprocess
import threading
import time

from logs import log_event


class AwsError(Exception):
    pass


# ---- per-bucket region ----
# Every s3api call that names a bucket is sent straight to that bucket's
# region (--region), so a bucket outside AWS_DEFAULT_REGION (e.g. a cheaper
# Stockholm one next to Frankfurt ones) costs no redirect. On the master,
# `lookup` (buckets.lookup_region) asks S3 once per bucket and keeps the
# answer; agents get the master's map with their policy. A bucket whose region
# isn't known yet still works: the CLI follows S3's redirect by itself.
REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d+$")
REGIONS = {}            # bucket -> region
lookup = None           # master only: callable(bucket) -> region | None
_looking = set()        # buckets whose lookup is running (its own call skips this)
_rlock = threading.Lock()


def set_regions(mapping):
    """Merge {bucket: region} (e.g. from the master's policy)."""
    with _rlock:
        REGIONS.update({b: r for b, r in (mapping or {}).items()
                        if isinstance(r, str) and REGION_RE.match(r)})


def bucket_region(bucket):
    """The bucket's region if known — looked up now on the master if not."""
    r = REGIONS.get(bucket)
    if r or not lookup or not bucket:
        return r
    with _rlock:
        if bucket in _looking:
            return None
        _looking.add(bucket)
    try:
        return lookup(bucket)
    finally:
        with _rlock:
            _looking.discard(bucket)


def _with_region(args):
    if not args or args[0] != "s3api" or "--region" in args or "--bucket" not in args:
        return args
    i = args.index("--bucket")
    region = bucket_region(args[i + 1]) if i + 1 < len(args) else None
    return (*args, "--region", region) if region else args


def aws(*args, log=True, log_errors=True, timeout=None, creds=None):
    """Run an aws CLI command, log it, return parsed JSON output (or {}).

    `log_errors=False` is for calls whose failure is an expected answer rather
    than a fault (the delete-permission probe): the caller logs the conclusion,
    so an ERROR row here would just cry wolf.

    `creds` = {AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN} runs
    this one command as someone else (the delete role) instead of the everyday
    user. They go in the child's environment only — never on the command line,
    so never in a log."""
    cmd = ["aws", *_with_region(args), "--output", "json"]
    pretty = " ".join(shlex.quote(c) for c in cmd)
    env = None
    if creds:
        env = {k: v for k, v in os.environ.items() if k != "AWS_PROFILE"}
        env.update(creds)
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError:
        raise AwsError("aws CLI not found — is it installed in the container?")
    except subprocess.TimeoutExpired:
        if log_errors:
            log_event("ERROR", "aws", f"timeout after {timeout}s: {pretty}")
        raise AwsError(f"aws command timed out: {pretty}")
    dur = time.time() - t0
    if p.returncode != 0:
        err = (p.stderr or "").strip()
        if log_errors:
            log_event("ERROR", "aws", f"failed (exit {p.returncode}, {dur:.1f}s): {pretty}",
                      err[:2000])
        raise AwsError(err or f"aws exited {p.returncode}")
    if log:
        log_event("DEBUG", "aws", f"ok ({dur:.1f}s): {pretty}")
    out = (p.stdout or "").strip()
    if not out:
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"raw": out}


def s3api(*args, **kw):
    return aws("s3api", *args, **kw)


def list_all_objects(bucket, prefix="", timeout=600):
    """Yield every object dict in a bucket, following pagination tokens
    (list-objects-v2 returns at most 1000 keys per call)."""
    token = None
    while True:
        args = ["list-objects-v2", "--bucket", bucket, "--max-items", "1000"]
        if prefix:
            args += ["--prefix", prefix.rstrip("/") + "/"]
        if token:
            args += ["--starting-token", token]
        resp = s3api(*args, timeout=timeout, log=False)
        for obj in resp.get("Contents", []):
            yield obj
        token = resp.get("NextToken")
        if not token:
            break


def quiet_abort(bucket, key, upload_id):
    """Best-effort abort of a multipart upload; never raises."""
    try:
        s3api("abort-multipart-upload", "--bucket", bucket, "--key", key, "--upload-id", upload_id)
    except AwsError:
        pass
