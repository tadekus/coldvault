import json
import shlex
import subprocess
import time

from logs import log_event


class AwsError(Exception):
    pass


def aws(*args, log=True, timeout=None):
    """Run an aws CLI command, log it, return parsed JSON output (or {})."""
    cmd = ["aws", *args, "--output", "json"]
    pretty = " ".join(shlex.quote(c) for c in cmd)
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise AwsError("aws CLI not found — is it installed in the container?")
    except subprocess.TimeoutExpired:
        log_event("ERROR", "aws", f"timeout after {timeout}s: {pretty}")
        raise AwsError(f"aws command timed out: {pretty}")
    dur = time.time() - t0
    if p.returncode != 0:
        err = (p.stderr or "").strip()
        log_event("ERROR", "aws", f"failed (exit {p.returncode}, {dur:.1f}s): {pretty}", err[:2000])
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


def quiet_abort(bucket, key, upload_id):
    """Best-effort abort of a multipart upload; never raises."""
    try:
        s3api("abort-multipart-upload", "--bucket", bucket, "--key", key, "--upload-id", upload_id)
    except AwsError:
        pass
