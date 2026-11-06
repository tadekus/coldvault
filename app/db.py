import json
import os
import sqlite3
import threading
from datetime import datetime

import config

_lock = threading.RLock()   # re-entrant: claim_upload checks and writes under one hold
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY,
  bucket TEXT NOT NULL,
  key TEXT NOT NULL,
  local_path TEXT,
  size INTEGER,
  mtime REAL,
  sha256 TEXT,
  checksum_s3 TEXT,
  etag TEXT,
  storage_class TEXT,
  status TEXT,              -- uploading | verified | failed | remote
  error TEXT,
  session_id INTEGER,
  uploaded_at TEXT,
  verified_at TEXT,
  upload_seconds REAL,
  audit_state TEXT,        -- ok | missing | size_mismatch | class_drift (last audit)
  audited_at TEXT,
  manifest_state TEXT,     -- ok | mismatch | not_in_manifest | algo_unsupported
  manifest_algo TEXT,
  manifest_hash TEXT,
  expires_at TEXT,         -- planned deletion date (ColdVault-managed retention)
  UNIQUE(bucket, key)
);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_session ON files(session_id);
CREATE INDEX IF NOT EXISTS idx_files_sha ON files(bucket, sha256);

CREATE TABLE IF NOT EXISTS sessions(
  id INTEGER PRIMARY KEY,
  bucket TEXT,
  source TEXT,
  label TEXT,
  trigger TEXT,             -- canary | manual
  status TEXT,              -- queued | scanning | running | done | done_with_errors | failed
  started_at TEXT,
  finished_at TEXT,
  total_files INTEGER DEFAULT 0,
  done_files INTEGER DEFAULT 0,
  skipped_files INTEGER DEFAULT 0,
  failed_files INTEGER DEFAULT 0,
  total_bytes INTEGER DEFAULT 0,
  done_bytes INTEGER DEFAULT 0,
  location TEXT DEFAULT '', -- '' = this master; else the agent that runs it
  items TEXT                -- JSON list of selected paths (manual selection)
);

CREATE TABLE IF NOT EXISTS restores(
  id INTEGER PRIMARY KEY,
  bucket TEXT,
  key TEXT NOT NULL,
  tier TEXT,
  days INTEGER,
  status TEXT,              -- in_progress | completed | failed
  requested_at TEXT,
  last_checked TEXT,
  expiry TEXT,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_restores_key ON restores(key);

CREATE TABLE IF NOT EXISTS dl_sessions(
  id INTEGER PRIMARY KEY,
  dest TEXT,
  status TEXT,              -- queued | running | done | done_with_errors | failed
  started_at TEXT,
  finished_at TEXT,
  total_files INTEGER DEFAULT 0,
  done_files INTEGER DEFAULT 0,
  skipped_files INTEGER DEFAULT 0,
  failed_files INTEGER DEFAULT 0,
  total_bytes INTEGER DEFAULT 0,
  done_bytes INTEGER DEFAULT 0,
  location TEXT DEFAULT '', -- '' = this master; else the agent that runs it
  items TEXT                -- JSON list of the objects to fetch
);

CREATE TABLE IF NOT EXISTS downloads(
  id INTEGER PRIMARY KEY,
  session_id INTEGER,
  bucket TEXT,
  key TEXT,
  local_path TEXT,
  size INTEGER,
  status TEXT,              -- downloading | verified | downloaded | skipped | failed
  error TEXT,
  sha256 TEXT,
  download_seconds REAL,
  finished_at TEXT,
  manifest_state TEXT      -- ok | mismatch | algo_unsupported (vs offload manifest)
);
CREATE INDEX IF NOT EXISTS idx_downloads_key ON downloads(bucket, key);
CREATE INDEX IF NOT EXISTS idx_downloads_session ON downloads(session_id);

-- Permanent audit trail of deleted objects. Rows here OUTLIVE the files row,
-- so the archive can always answer "what was removed, when, by which action".
CREATE TABLE IF NOT EXISTS deletions(
  id INTEGER PRIMARY KEY,
  bucket TEXT,
  key TEXT,
  size INTEGER,
  sha256 TEXT,
  manifest_hash TEXT,
  uploaded_at TEXT,
  expires_at TEXT,
  deleted_at TEXT,
  reason TEXT,
  mode TEXT,               -- manual | expiry
  early_days INTEGER       -- days short of the storage-class minimum (0 = none)
);
CREATE INDEX IF NOT EXISTS idx_deletions_bucket ON deletions(bucket, deleted_at);

-- Second machines (agents) that have checked in. Kept so the dashboard can show
-- an agent as offline after a master restart, instead of forgetting it.
CREATE TABLE IF NOT EXISTS agents(
  name TEXT PRIMARY KEY,
  boot_id TEXT,
  version TEXT,
  first_seen TEXT,
  last_seen TEXT,
  info TEXT                 -- JSON: drives, activity, free space (last check-in)
);

CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY,
  ts TEXT,
  level TEXT,
  category TEXT,
  message TEXT,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


TIME_FMT = "%Y-%m-%d %H:%M:%S"


def now():
    # Local system time. The container inherits the host timezone via the
    # /etc/localtime mount (and/or TZ) in docker-compose; see .env.example.
    return datetime.now().strftime(TIME_FMT)


def to_local(value):
    """Convert an S3 timestamp to local time in our display format.
    Handles ISO 8601 ('2026-07-18T19:08:16+00:00', '…Z') and RFC 1123
    ('Fri, 31 Jul 2027 00:00:00 GMT'). Returns the input unchanged if it
    can't be parsed, and passes through values already in our format."""
    if not value:
        return value
    s = str(value).strip()
    try:  # already local / our format
        datetime.strptime(s, TIME_FMT)
        return s
    except ValueError:
        pass
    dt = None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(s)
        except Exception:
            return s
    if dt is None:
        return s
    if dt.tzinfo is not None:
        dt = dt.astimezone()          # -> local timezone
    return dt.strftime(TIME_FMT)


def _migrate():
    """Upgrade a pre-multi-bucket database in place: adds the bucket column
    everywhere, backfilling with the bucket that was active at the time."""
    tables = {r[0] for r in _conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "files" not in tables:
        return
    cols = [r[1] for r in _conn.execute("PRAGMA table_info(files)").fetchall()]
    if "bucket" in cols:
        for col, decl in (("upload_seconds", "REAL"),
                          ("audit_state", "TEXT"),
                          ("audited_at", "TEXT"),
                          ("manifest_state", "TEXT"),
                          ("manifest_algo", "TEXT"),
                          ("manifest_hash", "TEXT"),
                          ("expires_at", "TEXT")):
            if col not in cols:
                print(f"[db] adding {col} column to files")
                _conn.execute(f"ALTER TABLE files ADD COLUMN {col} {decl}")
        dcols = [r[1] for r in _conn.execute("PRAGMA table_info(downloads)").fetchall()]
        if dcols and "manifest_state" not in dcols:
            print("[db] adding manifest_state column to downloads")
            _conn.execute("ALTER TABLE downloads ADD COLUMN manifest_state TEXT")
        _conn.commit()
        return
    default = None
    if "settings" in tables:
        row = _conn.execute("SELECT value FROM settings WHERE key='bucket'").fetchone()
        default = row[0] if row else None
    default = default or config.BUCKET or "unknown"
    print(f"[db] migrating database to multi-bucket schema (existing rows -> '{default}')")
    _conn.executescript("""
        DROP INDEX IF EXISTS idx_files_key;
        DROP INDEX IF EXISTS idx_files_status;
        DROP INDEX IF EXISTS idx_files_session;
        ALTER TABLE files RENAME TO files_old;
    """)
    _conn.executescript(SCHEMA)
    _conn.execute("""INSERT OR IGNORE INTO files(bucket, key, local_path, size, mtime,
                       sha256, checksum_s3, etag, storage_class, status, error,
                       session_id, uploaded_at, verified_at)
                     SELECT ?, key, local_path, size, mtime, sha256, checksum_s3, etag,
                       storage_class, status, error, session_id, uploaded_at, verified_at
                     FROM files_old""", (default,))
    _conn.execute("DROP TABLE files_old")
    for table in ("restores", "sessions"):
        tcols = [r[1] for r in _conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if "bucket" not in tcols:
            _conn.execute(f"ALTER TABLE {table} ADD COLUMN bucket TEXT")
            _conn.execute(f"UPDATE {table} SET bucket=?", (default,))
    _conn.commit()


def _ensure_columns():
    """Columns added after a table first shipped (CREATE TABLE IF NOT EXISTS
    leaves an existing table alone)."""
    for table, col, decl in (("sessions", "location", "TEXT DEFAULT ''"),
                             ("sessions", "items", "TEXT"),
                             ("dl_sessions", "location", "TEXT DEFAULT ''"),
                             ("dl_sessions", "items", "TEXT")):
        cols = [r[1] for r in _conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if col not in cols:
            print(f"[db] adding {col} column to {table}")
            _conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    _conn.commit()


def init():
    global _conn
    _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _migrate()
    _conn.executescript(SCHEMA)
    _ensure_columns()
    _conn.commit()


def snapshot(path):
    """Write a consistent copy of the live database to `path` using SQLite's
    online backup API — unlike copying coldvault.db, this can never capture a
    half-written page. Writers wait on the lock for the (short) duration."""
    dest = sqlite3.connect(path)
    try:
        with _lock:
            _conn.backup(dest)
        dest.commit()
    finally:
        dest.close()


def _exec(sql, params=()):
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur


def _rows(sql, params=()):
    with _lock:
        return [dict(r) for r in _conn.execute(sql, params).fetchall()]


def _row(sql, params=()):
    rows = _rows(sql, params)
    return rows[0] if rows else None


# ---- settings ----

def get_setting(key):
    r = _row("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else None


def set_setting(key, value):
    _exec("INSERT INTO settings(key,value) VALUES(?,?) "
          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# ---- events ----

_event_count = 0


def insert_event(level, category, message, detail=None, ts=None):
    """`ts` keeps an agent's own timestamp on a line it forwarded later."""
    global _event_count
    _exec("INSERT INTO events(ts, level, category, message, detail) VALUES(?,?,?,?,?)",
          (ts or now(), level, category, message, detail))
    _event_count += 1
    if _event_count % 1000 == 0:
        _exec("DELETE FROM events WHERE id < (SELECT COALESCE(MAX(id),0) FROM events) - 100000")


def list_events(level=None, category=None, q=None, limit=200):
    sql = "SELECT * FROM events WHERE 1=1"
    params = []
    if level:
        sql += " AND level = ?"
        params.append(level)
    if category:
        sql += " AND category = ?"
        params.append(category)
    if q:
        sql += " AND (message LIKE ? OR detail LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(min(int(limit), 2000))
    return _rows(sql, params)


# ---- sessions ----

def create_session(bucket, source, label, trigger, location="", items=None):
    cur = _exec("INSERT INTO sessions(bucket, source, label, trigger, status, started_at, "
                "location, items) VALUES(?,?,?,?,?,?,?,?)",
                (bucket, source, label, trigger, "queued", now(), location or "",
                 json.dumps(items) if items is not None else None))
    return cur.lastrowid


def update_session(sid, **kw):
    # A session stopped by hand stays "stopped": the machine running it may
    # still report scanning/running/done before it notices.
    if kw.get("status") not in (None, "stopped"):
        cur = _row("SELECT status FROM sessions WHERE id=?", (sid,))
        if cur and cur["status"] == "stopped":
            kw.pop("status")
            kw.pop("finished_at", None)
    if not kw:
        return
    cols = ", ".join(f"{k}=?" for k in kw)
    _exec(f"UPDATE sessions SET {cols} WHERE id=?", (*kw.values(), sid))


def stop_session(sid):
    """Stop an upload session by hand: a queued one never starts; a running one
    starts no new file, and a large file in progress is abandoned between parts
    (its multipart upload aborted). Files already verified stay. Returns the
    session as it was, or None if it isn't queued or running."""
    with _lock:
        s = get_session(sid)
        if not s or s["status"] not in ACTIVE:
            return None
        _exec("UPDATE sessions SET status='stopped', finished_at=? WHERE id=?", (now(), sid))
        return s


def bump_session(sid, done=0, skipped=0, failed=0, bytes_done=0):
    _exec("""UPDATE sessions SET done_files=done_files+?, skipped_files=skipped_files+?,
             failed_files=failed_files+?, done_bytes=done_bytes+? WHERE id=?""",
          (done, skipped, failed, bytes_done, sid))


def get_session(sid):
    return _row("SELECT * FROM sessions WHERE id=?", (sid,))


def list_sessions(limit=50):
    return _rows("SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (limit,))


def latest_session_status(source, bucket, location=""):
    r = _row("SELECT status FROM sessions WHERE source=? AND bucket=? AND location=? "
             "ORDER BY id DESC LIMIT 1", (source, bucket, location or ""))
    return r["status"] if r else None


ACTIVE = ("queued", "scanning", "running")


def fail_stale_sessions():
    """Mark this master's own sessions left mid-flight by a crash/restart as
    failed, so the UI doesn't show a 'running' session no process is running.
    An agent's sessions are left alone: the agent is still running them."""
    cur = _exec("UPDATE sessions SET status='failed', finished_at=? "
                "WHERE location='' AND status IN ('queued','scanning','running')", (now(),))
    return cur.rowcount


def session_location(sid):
    r = _row("SELECT location FROM sessions WHERE id=?", (sid,))
    return r["location"] if r else None


def queued_sessions(location):
    return [r["id"] for r in _rows(
        "SELECT id FROM sessions WHERE location=? AND status='queued' ORDER BY id",
        (location,))]


# ---- files ----

def get_file(bucket, key):
    return _row("SELECT * FROM files WHERE bucket=? AND key=?", (bucket, key))


def upsert_file(bucket, key, **kw):
    existing = get_file(bucket, key)
    if existing:
        cols = ", ".join(f"{k}=?" for k in kw)
        _exec(f"UPDATE files SET {cols} WHERE bucket=? AND key=?",
              (*kw.values(), bucket, key))
        return existing["id"]
    cols = ["bucket", "key"] + list(kw.keys())
    marks = ",".join("?" * len(cols))
    cur = _exec(f"INSERT INTO files({','.join(cols)}) VALUES({marks})",
                (bucket, key, *kw.values()))
    return cur.lastrowid


def claim_upload(bucket, key, sid, sha256=None, **fields):
    """Reserve (bucket, key) for session `sid` just before its upload starts,
    so two machines never write the same object at once. Atomic: the check and
    the write happen under one hold of the lock.

    Returns {"state": "ok"} once reserved, {"state": "busy", "holder": id,
    "location": …} while another still-running session holds the key,
    {"state": "conflict", "location": …} when the key already holds different
    content uploaded from another machine, or {"state": "inactive"} if `sid`
    itself is no longer running (it was failed — e.g. its agent was offline too
    long — so it must stop).

    The conflict rule: replacing a changed file is normal on the machine that
    uploaded it, but a *different* machine writing different bytes to the same
    key is almost always two drives given the same label — refused rather than
    letting one silently replace the other (on Deep Archive the replaced object
    would still be billed its full minimum). `sha256` is the new content's hash
    when known; without it, a size match is required."""
    with _lock:
        s = _row("SELECT status, location FROM sessions WHERE id=?", (sid,))
        if not s or s["status"] not in ACTIVE:
            return {"state": "inactive"}
        r = get_file(bucket, key)
        if r and r["session_id"] not in (None, sid):
            h = _row("SELECT status, location FROM sessions WHERE id=?", (r["session_id"],))
            if r["status"] == "uploading" and h and h["status"] in ACTIVE:
                return {"state": "busy", "holder": r["session_id"],
                        "location": h["location"] or ""}
            if (r["status"] == "verified" and h and (h["location"] or "") != (s["location"] or "")
                    and (r["sha256"] != sha256 if sha256 else r["size"] != fields.get("size"))):
                return {"state": "conflict", "location": h["location"] or ""}
        upsert_file(bucket, key, session_id=sid, status="uploading", error=None, **fields)
        return {"state": "ok"}


def preview_rows(bucket, keys):
    """For an upload preview: the index row of each key that has one, with the
    machine and state of the session that last wrote it. {key: row}"""
    out, keys = {}, list(keys)
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        for r in _rows(f"""SELECT f.key, f.status, f.error, f.size, f.mtime, f.session_id,
                                  s.location AS s_location, s.status AS s_status
                           FROM files f LEFT JOIN sessions s ON s.id = f.session_id
                           WHERE f.bucket=? AND f.key IN ({','.join('?' * len(chunk))})""",
                       [bucket] + chunk):
            out[r["key"]] = r
    return out


def verified_sizes(bucket):
    """{size: one key} over the bucket's verified objects — for the preview's
    'may be a duplicate' hint (only a checksum can tell for sure)."""
    return {r["size"]: r["key"] for r in _rows(
        "SELECT size, MIN(key) AS key FROM files WHERE bucket=? AND status='verified' "
        "GROUP BY size", (bucket,))}


def find_duplicate(bucket, sha256, size, exclude_key):
    """A verified object in the same bucket with identical content."""
    return _row("""SELECT key FROM files WHERE bucket=? AND sha256=? AND size=?
                   AND status='verified' AND key != ? LIMIT 1""",
                (bucket, sha256, size, exclude_key))


def search_files(bucket=None, q=None, status=None, session_id=None, sort="new",
                 manifest=None, limit=100, offset=0):
    where, params = "WHERE 1=1", []
    if bucket:
        where += " AND bucket = ?"
        params.append(bucket)
    if q:
        for term in q.split():
            where += " AND key LIKE ?"
            params.append(f"%{term}%")
    if status:
        where += " AND status = ?"
        params.append(status)
    if manifest:
        where += " AND manifest_state = ?"
        params.append(manifest)
    if session_id:
        where += " AND session_id = ?"
        params.append(session_id)
    # "new" = most recently indexed/uploaded first (id is insertion order, and is
    # format-independent unlike the mixed uploaded_at timestamps); "key" = A–Z.
    order = "id DESC" if sort == "new" else "bucket, key"
    total = _row(f"SELECT COUNT(*) c, COALESCE(SUM(size),0) b FROM files {where}", params)
    items = _rows(f"SELECT * FROM files {where} ORDER BY {order} LIMIT ? OFFSET ?",
                  params + [min(int(limit), 500), int(offset)])
    return total["c"], total["b"], items


def match_files_by_name(bucket, name):
    """Find indexed objects whose filename (last key segment) equals name,
    case-insensitively. bucket=None searches every bucket."""
    esc = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    where = "WHERE (key LIKE ? ESCAPE '\\' OR key = ? COLLATE NOCASE)"
    params = [f"%/{esc}", name]
    if bucket:
        where += " AND bucket = ?"
        params.append(bucket)
    return _rows(f"SELECT bucket, key, size, status FROM files {where} LIMIT 20", params)


def files_for_audit(bucket):
    """All index rows for a bucket that represent objects expected in S3."""
    return _rows("SELECT id, key, size, storage_class, status FROM files "
                 "WHERE bucket=? AND status IN ('verified','remote')", (bucket,))


def set_audit(file_id, state, when):
    _exec("UPDATE files SET audit_state=?, audited_at=? WHERE id=?",
          (state, when, file_id))


def clear_audit(bucket):
    _exec("UPDATE files SET audit_state=NULL WHERE bucket=?", (bucket,))


def manifest_summary(bucket):
    """Counts of files by manifest_state for a bucket (only rows that have one)."""
    return {r["manifest_state"]: r["c"] for r in _rows(
        "SELECT manifest_state, COUNT(*) c FROM files "
        "WHERE bucket=? AND manifest_state IS NOT NULL GROUP BY manifest_state", (bucket,))}


def manifest_mismatch_keys(bucket, limit=200):
    return [r["key"] for r in _rows(
        "SELECT key FROM files WHERE bucket=? AND manifest_state='mismatch' "
        "ORDER BY key LIMIT ?", (bucket, limit))]


def files_by_basename(bucket, name, scope=None, limit=50):
    """Full index rows whose filename (last key segment) equals name
    (case-insensitive). bucket=None searches all buckets; scope optionally
    restricts to keys containing that substring (e.g. a shoot folder)."""
    def esc(s):
        return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    where = "WHERE (key LIKE ? ESCAPE '\\' OR key = ? COLLATE NOCASE)"
    params = [f"%/{esc(name)}", name]
    if bucket:
        where += " AND bucket = ?"
        params.append(bucket)
    if scope:
        where += " AND key LIKE ? ESCAPE '\\'"
        params.append(f"%{esc(scope)}%")
    return _rows(f"SELECT * FROM files {where} LIMIT ?", params + [limit])


def files_by_stem(bucket, stem, scope=None, limit=50):
    """Index rows whose filename WITHOUT extension equals stem — for manifests
    that list bare clip names (e.g. Silverstack 'A_0002C003_…_h1DPN' for the
    archived '…_h1DPN.mxf', or '4-10T01' for '4-10T01.wav')."""
    def esc(s):
        return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    where = "WHERE (key LIKE ? ESCAPE '\\' OR key LIKE ? ESCAPE '\\')"
    params = [f"%/{esc(stem)}.%", f"{esc(stem)}.%"]
    if bucket:
        where += " AND bucket = ?"
        params.append(bucket)
    if scope:
        where += " AND key LIKE ? ESCAPE '\\'"
        params.append(f"%{esc(scope)}%")
    rows = _rows(f"SELECT * FROM files {where} LIMIT ?", params + [limit])
    s = stem.lower()
    # LIKE '%/stem.%' would also accept 'stem.foo.wav'; keep exact stem matches only
    return [r for r in rows
            if os.path.splitext(r["key"].rsplit("/", 1)[-1])[0].lower() == s]


def set_manifest(file_id, state, algo, mhash):
    _exec("UPDATE files SET manifest_state=?, manifest_algo=?, manifest_hash=? WHERE id=?",
          (state, algo, mhash, file_id))


def get_file_by_id(file_id):
    return _row("SELECT * FROM files WHERE id=?", (file_id,))


# ---- tree browsing ----

def _esc_like(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def tree_children(bucket, prefix="", file_limit=1000):
    """Immediate children of a key prefix: sub-folders (with aggregate counts and
    sizes for the whole subtree) and the files sitting directly at this level."""
    plen = len(prefix)
    like = f"{_esc_like(prefix)}%"
    folders = _rows(
        "SELECT substr(k, 1, instr(k, '/')) AS name, COUNT(*) c, "
        "       COALESCE(SUM(size),0) b "
        "FROM (SELECT substr(key, ?+1) AS k, size FROM files "
        "      WHERE bucket=? AND key LIKE ? ESCAPE '\\') "
        "WHERE instr(k, '/') > 0 GROUP BY name ORDER BY name",
        (plen, bucket, like))
    files = _rows(
        "SELECT * FROM files WHERE bucket=? AND key LIKE ? ESCAPE '\\' "
        "AND instr(substr(key, ?+1), '/') = 0 ORDER BY key LIMIT ?",
        (bucket, like, plen, file_limit))
    return folders, files


def bucket_totals():
    """Every indexed bucket with its object count and bytes — the roots of the
    Tree view."""
    return _rows("SELECT bucket, COUNT(*) c, COALESCE(SUM(size),0) b "
                 "FROM files GROUP BY bucket ORDER BY bucket")


def files_for_export(bucket, prefix="", limit=500000):
    """Every indexed object under a prefix, key-ordered — the input for the
    tree export's folder roll-up."""
    return _rows(
        "SELECT key, size, status, storage_class, uploaded_at, verified_at, "
        "       sha256, manifest_state, expires_at "
        "FROM files WHERE bucket=? AND key LIKE ? ESCAPE '\\' ORDER BY key LIMIT ?",
        (bucket, f"{_esc_like(prefix)}%", limit))


def keys_under(bucket, prefixes=None, keys=None, limit=200000):
    """All index rows covered by a set of prefixes and/or explicit keys."""
    clauses, params = [], []
    for p in (prefixes or []):
        clauses.append("key LIKE ? ESCAPE '\\'")
        params.append(f"{_esc_like(p)}%")
    if keys:
        clauses.append(f"key IN ({','.join('?' * len(keys))})")
        params += list(keys)
    if not clauses:
        return []
    return _rows(f"SELECT * FROM files WHERE bucket=? AND ({' OR '.join(clauses)}) "
                 f"ORDER BY key LIMIT ?", [bucket] + params + [limit])


# ---- retention / deletion ----

def files_for_retention(bucket, prefix=None, keys=None, limit=100000):
    where, params = "WHERE bucket=?", [bucket]
    if prefix:
        esc = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where += " AND key LIKE ? ESCAPE '\\'"
        params.append(f"{esc}%")
    if keys:
        where += f" AND key IN ({','.join('?' * len(keys))})"
        params += list(keys)
    return _rows(f"SELECT * FROM files {where} ORDER BY key LIMIT ?", params + [limit])


def set_expiry(bucket, keys, when):
    """Set (or clear, when None) the planned deletion date on specific keys."""
    n = 0
    for k in keys:
        n += _exec("UPDATE files SET expires_at=? WHERE bucket=? AND key=?",
                   (when, bucket, k)).rowcount
    return n


def _in_bucket(bucket):
    """WHERE fragment for an optional bucket filter: None means every bucket."""
    return ("bucket=? AND ", [bucket]) if bucket else ("", [])


def files_due(bucket, as_of):
    w, p = _in_bucket(bucket)
    return _rows(f"SELECT * FROM files WHERE {w}expires_at IS NOT NULL "
                 f"AND expires_at <= ? ORDER BY expires_at, bucket, key", p + [as_of])


def files_scheduled(bucket, limit=1000):
    """Everything with a planned deletion date — due or not — soonest first.
    `files_due` only answers 'what can I delete now'; this answers 'what have I
    scheduled', which is what you want right after setting an expiry.
    bucket=None covers every bucket."""
    w, p = _in_bucket(bucket)
    return _rows(f"SELECT * FROM files WHERE {w}expires_at IS NOT NULL "
                 f"ORDER BY expires_at, bucket, key LIMIT ?", p + [limit])


def next_expiry(bucket):
    w, p = _in_bucket(bucket)
    r = _row(f"SELECT MIN(expires_at) e FROM files WHERE {w}expires_at IS NOT NULL", p)
    return r["e"] if r else None


def count_scheduled(bucket):
    w, p = _in_bucket(bucket)
    r = _row(f"SELECT COUNT(*) c, COALESCE(SUM(size),0) b FROM files "
             f"WHERE {w}expires_at IS NOT NULL", p)
    return r["c"], r["b"]


def record_deletion(row, reason, mode, early_days):
    _exec("""INSERT INTO deletions(bucket, key, size, sha256, manifest_hash,
             uploaded_at, expires_at, deleted_at, reason, mode, early_days)
             VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
          (row["bucket"], row["key"], row["size"], row.get("sha256"),
           row.get("manifest_hash"), row.get("uploaded_at"), row.get("expires_at"),
           now(), reason, mode, early_days))


def drop_file(bucket, key):
    return _exec("DELETE FROM files WHERE bucket=? AND key=?", (bucket, key)).rowcount


def list_deletions(bucket=None, limit=300):
    where, params = "", []
    if bucket:
        where = "WHERE bucket=?"
        params = [bucket]
    return _rows(f"SELECT * FROM deletions {where} ORDER BY id DESC LIMIT ?",
                 params + [limit])


def deletion_totals(bucket=None):
    where, params = "", []
    if bucket:
        where = "WHERE bucket=?"
        params = [bucket]
    r = _row(f"SELECT COUNT(*) c, COALESCE(SUM(size),0) b, "
             f"COALESCE(SUM(CASE WHEN early_days>0 THEN 1 ELSE 0 END),0) early "
             f"FROM deletions {where}", params)
    return {"count": r["c"], "bytes": r["b"], "early": r["early"]}


def audit_issue_counts(bucket):
    return {r["audit_state"]: r["c"] for r in _rows(
        "SELECT audit_state, COUNT(*) c FROM files WHERE bucket=? AND "
        "audit_state IN ('missing','size_mismatch','class_drift') GROUP BY audit_state",
        (bucket,))}


def restore_summary():
    r = _row("SELECT SUM(status='in_progress') ip, SUM(status='completed') done, "
             "SUM(status='failed') failed, MAX(last_checked) lc FROM restores")
    return {"in_progress": r["ip"] or 0, "completed": r["done"] or 0,
            "failed": r["failed"] or 0, "last_checked": r["lc"]}


def latest_finished_session(bucket):
    return _row("SELECT * FROM sessions WHERE bucket=? AND "
                "status IN ('done','done_with_errors','failed') ORDER BY id DESC LIMIT 1",
                (bucket,))


def distinct_buckets():
    return [r["bucket"] for r in _rows(
        "SELECT DISTINCT bucket FROM files ORDER BY bucket")]


def uploading_files(limit=20):
    return _rows("SELECT f.bucket, f.key, f.size, f.session_id, "
                 "       COALESCE(s.location, '') AS location "
                 "FROM files f LEFT JOIN sessions s ON s.id = f.session_id "
                 "WHERE f.status='uploading' ORDER BY f.id LIMIT ?", (limit,))


def fail_stale_uploads():
    """Mark files this master left in 'uploading' by a crash/restart as failed
    so the next session retries them (and the UI doesn't show ghost uploads).
    Files an agent is uploading are left alone."""
    cur = _exec("UPDATE files SET status='failed', error='interrupted by restart' "
                "WHERE status='uploading' AND (session_id IS NULL OR session_id NOT IN "
                "(SELECT id FROM sessions WHERE location <> ''))")
    return cur.rowcount


def stats(bucket=None):
    where, params = "", []
    if bucket:
        where = "WHERE bucket=?"
        params = [bucket]
    by_status = {r["status"]: {"count": r["c"], "bytes": r["b"]}
                 for r in _rows(f"SELECT status, COUNT(*) c, COALESCE(SUM(size),0) b "
                                f"FROM files {where} GROUP BY status", params)}
    sessions = _row("SELECT COUNT(*) c FROM sessions")["c"]
    active_restores = _row("SELECT COUNT(*) c FROM restores WHERE status='in_progress'")["c"]
    return {"files": by_status, "sessions": sessions, "active_restores": active_restores}


# ---- downloads ----

def create_dl_session(dest, location="", items=None):
    cur = _exec("INSERT INTO dl_sessions(dest, status, started_at, location, items) "
                "VALUES(?,?,?,?,?)",
                (dest, "queued", now(), location or "",
                 json.dumps(items) if items is not None else None))
    return cur.lastrowid


def dl_session_location(sid):
    r = _row("SELECT location FROM dl_sessions WHERE id=?", (sid,))
    return r["location"] if r else None


def download_location(did):
    r = _row("SELECT s.location FROM downloads d JOIN dl_sessions s ON s.id = d.session_id "
             "WHERE d.id=?", (did,))
    return r["location"] if r else None


def queued_dl_sessions(location):
    return [r["id"] for r in _rows(
        "SELECT id FROM dl_sessions WHERE location=? AND status='queued' ORDER BY id",
        (location,))]


def update_dl_session(sid, **kw):
    cols = ", ".join(f"{k}=?" for k in kw)
    _exec(f"UPDATE dl_sessions SET {cols} WHERE id=?", (*kw.values(), sid))


def bump_dl_session(sid, done=0, skipped=0, failed=0, bytes_done=0):
    _exec("""UPDATE dl_sessions SET done_files=done_files+?, skipped_files=skipped_files+?,
             failed_files=failed_files+?, done_bytes=done_bytes+? WHERE id=?""",
          (done, skipped, failed, bytes_done, sid))


def get_dl_session(sid):
    return _row("SELECT * FROM dl_sessions WHERE id=?", (sid,))


def list_dl_sessions(limit=50):
    return _rows("SELECT * FROM dl_sessions ORDER BY id DESC LIMIT ?", (limit,))


def add_download(session_id, bucket, key, local_path, size):
    cur = _exec("""INSERT INTO downloads(session_id, bucket, key, local_path, size, status)
                   VALUES(?,?,?,?,?,?)""",
                (session_id, bucket, key, local_path, size, "downloading"))
    return cur.lastrowid


def update_download(did, **kw):
    cols = ", ".join(f"{k}=?" for k in kw)
    _exec(f"UPDATE downloads SET {cols} WHERE id=?", (*kw.values(), did))


def list_downloads(session_id=None, limit=500):
    if session_id:
        return _rows("SELECT * FROM downloads WHERE session_id=? ORDER BY id DESC LIMIT ?",
                     (session_id, limit))
    return _rows("SELECT * FROM downloads ORDER BY id DESC LIMIT ?", (limit,))


_AT = "session_id IN (SELECT id FROM dl_sessions WHERE location=?)"


def downloaded_paths(location=""):
    """Every local path the index believes it wrote on this machine (or on
    agent `location`). Used to tell apart files on disk that ColdVault put there
    from ones nothing references any more."""
    return {r["local_path"] for r in _rows(
        f"SELECT DISTINCT local_path FROM downloads WHERE local_path IS NOT NULL AND {_AT}",
        (location or "",))}


def find_keys_any_bucket(keys, chunk=400):
    """key -> {bucket, size, status} for keys archived in ANY indexed bucket.

    A downloaded file's path under the destination *is* its S3 key, so this
    answers the question that matters before deleting a local copy: is this
    still in the archive, or is the copy on disk the last one?"""
    out = {}
    keys = list(keys)
    for i in range(0, len(keys), chunk):
        part = keys[i:i + chunk]
        for r in _rows(
                f"SELECT key, bucket, size, status FROM files "
                f"WHERE key IN ({','.join('?' * len(part))})", part):
            # prefer a verified row if the same key exists in several buckets
            if r["key"] not in out or r["status"] == "verified":
                out[r["key"]] = {"bucket": r["bucket"], "size": r["size"],
                                 "status": r["status"]}
    return out


def prune_orphan_dl_sessions():
    """Drop finished download sessions that have no file rows left.

    Clearing one bucket's index deletes its `downloads` rows, but `dl_sessions`
    has no bucket column — without this, the Downloads tab kept showing sessions
    claiming "5/5 files" while the file list underneath was empty."""
    return _exec(
        "DELETE FROM dl_sessions WHERE status NOT IN ('queued','running') "
        "AND id NOT IN (SELECT DISTINCT session_id FROM downloads "
        "               WHERE session_id IS NOT NULL)").rowcount


def latest_downloads_map(location=""):
    """(bucket, key) -> {local_path, status} of the most recent download (any
    status) to this machine, or to agent `location`. Later rows overwrite
    earlier, so the latest wins."""
    rows = _rows(f"SELECT bucket, key, local_path, status FROM downloads WHERE {_AT} "
                 f"ORDER BY id", (location or "",))
    return {(r["bucket"], r["key"]): {"local_path": r["local_path"], "status": r["status"]}
            for r in rows}


def downloaded_rows_to_check(location=""):
    """Download records that claim a local file exists, for integrity checking.
    'skipped' means the file was already present at download time."""
    return _rows(f"SELECT id, bucket, key, local_path FROM downloads "
                 f"WHERE status IN ('verified','downloaded','skipped') AND {_AT}",
                 (location or "",))


def fail_stale_downloads():
    """This master's downloads (and sessions) cut off by a restart. An agent's
    are left alone — the agent is still running them."""
    cur = _exec(f"UPDATE downloads SET status='failed', error='interrupted by restart' "
                f"WHERE status='downloading' AND {_AT}", ("",))
    _exec("UPDATE dl_sessions SET status='failed', finished_at=? "
          "WHERE location='' AND status IN ('queued','running')", (now(),))
    return cur.rowcount


def fail_location_work(location, reason):
    """An agent restarted, or was silent too long: whatever it had in flight
    is not going to finish. Fail its running upload/download sessions and the
    files inside them; queued sessions stay queued and are handed out again."""
    t = now()
    with _lock:
        sids = [r["id"] for r in _rows(
            "SELECT id FROM sessions WHERE location=? AND status IN ('scanning','running')",
            (location,))]
        dsids = [r["id"] for r in _rows(
            "SELECT id FROM dl_sessions WHERE location=? AND status='running'", (location,))]
        for sid in sids:
            _exec("UPDATE files SET status='failed', error=? "
                  "WHERE session_id=? AND status='uploading'", (reason, sid))
            _exec("UPDATE sessions SET status='failed', finished_at=? WHERE id=?", (t, sid))
        for dsid in dsids:
            _exec("UPDATE downloads SET status='failed', error=?, finished_at=? "
                  "WHERE session_id=? AND status='downloading'", (reason, t, dsid))
            _exec("UPDATE dl_sessions SET status='failed', finished_at=? WHERE id=?", (t, dsid))
    return sids, dsids


# ---- agents ----

def touch_agent(name, boot_id, version, info):
    """Record an agent check-in. Returns the boot id it had before (None the
    first time), so a restart of the agent can be noticed."""
    with _lock:
        prev = _row("SELECT boot_id FROM agents WHERE name=?", (name,))
        t = now()
        if prev:
            _exec("UPDATE agents SET boot_id=?, version=?, last_seen=?, info=? WHERE name=?",
                  (boot_id, version, t, json.dumps(info), name))
        else:
            _exec("INSERT INTO agents(name, boot_id, version, first_seen, last_seen, info) "
                  "VALUES(?,?,?,?,?,?)", (name, boot_id, version, t, t, json.dumps(info)))
        return prev["boot_id"] if prev else None


def list_agents():
    out = []
    for r in _rows("SELECT * FROM agents ORDER BY name"):
        try:
            r["info"] = json.loads(r["info"] or "{}")
        except ValueError:
            r["info"] = {}
        out.append(r)
    return out


def forget_agent(name):
    return _exec("DELETE FROM agents WHERE name=?", (name,)).rowcount


def location_activity(location):
    """Counts of sessions an agent still owes us: queued or in flight."""
    up = _row("SELECT COUNT(*) c FROM sessions WHERE location=? AND status IN "
              "('queued','scanning','running')", (location,))["c"]
    dl = _row("SELECT COUNT(*) c FROM dl_sessions WHERE location=? AND status IN "
              "('queued','running')", (location,))["c"]
    return {"uploads": up, "downloads": dl}


def restored_objects():
    """Objects whose most recent restore request completed, joined with what
    the index knows about them (size, checksum)."""
    return _rows("""
        SELECT r.bucket, r.key, r.tier, r.expiry, r.requested_at,
               f.size, f.sha256, f.status AS file_status
        FROM restores r
        JOIN (SELECT bucket, key, MAX(id) AS mid FROM restores GROUP BY bucket, key) t
          ON r.id = t.mid
        LEFT JOIN files f ON f.bucket = r.bucket AND f.key = r.key
        WHERE r.status = 'completed'
        ORDER BY r.bucket, r.key""")


# ---- maintenance ----

def clear_index(bucket):
    """Delete local index metadata. bucket=None/'*' wipes everything (files,
    sessions, restores, downloads); a name wipes just that bucket. Never touches
    S3 — only the local database. Settings and the event log are preserved.
    Returns per-table row counts removed."""
    counts = {}
    if bucket in (None, "*", ""):
        for t in ("files", "restores", "downloads", "sessions", "dl_sessions"):
            counts[t] = _exec(f"DELETE FROM {t}").rowcount
    else:
        counts["files"] = _exec("DELETE FROM files WHERE bucket=?", (bucket,)).rowcount
        counts["restores"] = _exec("DELETE FROM restores WHERE bucket=?", (bucket,)).rowcount
        counts["downloads"] = _exec("DELETE FROM downloads WHERE bucket=?", (bucket,)).rowcount
        counts["sessions"] = _exec("DELETE FROM sessions WHERE bucket=?", (bucket,)).rowcount
        # dl_sessions has no bucket column (a session can span buckets), so drop
        # the ones whose file rows we just removed instead of leaving them
        # claiming files the index no longer knows about.
        counts["dl_sessions"] = prune_orphan_dl_sessions()
    return counts


# ---- restores ----

def add_restore(bucket, key, tier, days, status, error=None):
    cur = _exec("""INSERT INTO restores(bucket, key, tier, days, status, requested_at, error)
                   VALUES(?,?,?,?,?,?,?)""",
                (bucket, key, tier, days, status, now(), error))
    return cur.lastrowid


def update_restore(rid, **kw):
    cols = ", ".join(f"{k}=?" for k in kw)
    _exec(f"UPDATE restores SET {cols} WHERE id=?", (*kw.values(), rid))


def list_restores(limit=200):
    return _rows("SELECT * FROM restores ORDER BY id DESC LIMIT ?", (limit,))


def pending_restores():
    return _rows("SELECT * FROM restores WHERE status='in_progress'")


def latest_restores_for_files(items):
    """Map (bucket, key) -> latest restore row for the given file rows."""
    keys = list({i["key"] for i in items})
    if not keys:
        return {}
    marks = ",".join("?" * len(keys))
    rows = _rows(f"SELECT * FROM restores WHERE key IN ({marks}) ORDER BY id", keys)
    return {(r["bucket"], r["key"]): r for r in rows}  # later rows win -> latest


init()
