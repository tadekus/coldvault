"""The master's database, reached over HTTP: what `db` is on an agent.

An agent keeps no database. Every read and write the uploader and downloader
make goes to the master through POST /api/agent/db, which accepts only the
calls defined here, and only on the agent's own sessions.

When the master can't be reached, a call doesn't fail: it waits and retries
until the master is back. That is what makes an agent stop when its master is
gone — the file in flight finishes, its record waits to be delivered, and
nothing new starts until it has been. The one exception is progress
counters (bytes moved so far), which are dropped rather than waited for.

Every call carries an id, and the master remembers recent answers by id, so a
call retried after a dropped connection is never applied twice.
"""

import json
import logging
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime

import config

TIME_FMT = "%Y-%m-%d %H:%M:%S"
PROTOCOL = 1

_log = logging.getLogger("coldvault")


class RemoteError(RuntimeError):
    """The master answered but refused (wrong token, not allowed, a bug).
    Retrying won't help."""


# For the agent's status page: is the master reachable, since when, why not.
status = {"connected": None, "since": None, "error": None}
_slock = threading.Lock()


def now():
    return datetime.now().strftime(TIME_FMT)


def _set(connected, error=None):
    with _slock:
        if status["connected"] != connected:
            status.update(connected=connected, since=now())
        status["error"] = error


def request(path, payload, timeout=60):
    """POST JSON to the master and return its decoded answer. Raises
    RemoteError when the master refuses, ConnectionError when it can't be
    reached at all."""
    req = urllib.request.Request(
        config.MASTER_URL + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {config.AGENT_TOKEN}",
                 "X-ColdVault-Agent": config.NAME,
                 "X-ColdVault-Protocol": str(PROTOCOL)})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read() or b"{}").get("error")
        except ValueError:
            msg = None
        err = f"master refused {path}: HTTP {e.code} {msg or e.reason}"
        _set(True, err)
        raise RemoteError(err)
    except OSError as e:   # URLError, refused, timeout, DNS — not there
        reason = getattr(e, "reason", e)
        _set(False, f"cannot reach {config.MASTER_URL}: {reason}")
        raise ConnectionError(str(reason))
    _set(True)
    return json.loads(body or b"{}")


def _call(fn, *args, best_effort=False, **kwargs):
    body = {"id": uuid.uuid4().hex, "fn": fn, "args": list(args), "kwargs": kwargs}
    delay, waiting = 2, False
    while True:
        try:
            result = request("/api/agent/db", body)["result"]
            if waiting:
                _log.info(f"[agent] master reachable again — {fn} delivered")
            return result
        except ConnectionError as e:
            if best_effort:
                return None
            if not waiting:
                _log.warning(f"[agent] master unreachable ({e}) — holding {fn} until it's back; "
                             f"nothing new starts meanwhile")
                waiting = True
            time.sleep(delay)
            delay = min(delay * 2, 30)


# ---- the calls the uploader and downloader make (same names as db.py) ----

def get_session(sid):
    return _call("get_session", sid)


def update_session(sid, **kw):
    return _call("update_session", sid, **kw)


def bump_session(sid, done=0, skipped=0, failed=0, bytes_done=0):
    # A file counted as done/failed must arrive; a progress tick may be dropped.
    return _call("bump_session", sid, done=done, skipped=skipped, failed=failed,
                 bytes_done=bytes_done, best_effort=not (done or skipped or failed))


def get_file(bucket, key):
    return _call("get_file", bucket, key)


def upsert_file(bucket, key, **kw):
    return _call("upsert_file", bucket, key, **kw)


def find_duplicate(bucket, sha256, size, exclude_key):
    return _call("find_duplicate", bucket, sha256, size, exclude_key)


def size_taken(bucket, size, exclude_key):
    return _call("size_taken", bucket, size, exclude_key)


def claim_upload(bucket, key, sid, **fields):
    return _call("claim_upload", bucket, key, sid, **fields)


def get_dl_session(sid):
    return _call("get_dl_session", sid)


def update_dl_session(sid, **kw):
    return _call("update_dl_session", sid, **kw)


def bump_dl_session(sid, done=0, skipped=0, failed=0, bytes_done=0):
    return _call("bump_dl_session", sid, done=done, skipped=skipped, failed=failed,
                 bytes_done=bytes_done, best_effort=not (done or skipped or failed))


def add_download(session_id, bucket, key, local_path, size):
    return _call("add_download", session_id, bucket, key, local_path, size)


def update_download(did, **kw):
    return _call("update_download", did, **kw)


# The master answers these for this agent's own downloads — `location` is
# accepted for signature compatibility and ignored.

def downloaded_paths(location=""):
    return set(_call("downloaded_paths"))


def downloaded_rows_to_check(location=""):
    return _call("downloaded_rows_to_check")


def find_keys_any_bucket(keys, chunk=400):
    return _call("find_keys_any_bucket", list(keys))
