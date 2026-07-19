import sqlite3
import threading
from datetime import datetime, timezone

import config

_lock = threading.Lock()
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
  done_bytes INTEGER DEFAULT 0
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


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _migrate():
    """Upgrade a pre-multi-bucket database in place: adds the bucket column
    everywhere, backfilling with the bucket that was active at the time."""
    tables = {r[0] for r in _conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "files" not in tables:
        return
    cols = [r[1] for r in _conn.execute("PRAGMA table_info(files)").fetchall()]
    if "bucket" in cols:
        if "upload_seconds" not in cols:
            print("[db] adding upload_seconds column to files")
            _conn.execute("ALTER TABLE files ADD COLUMN upload_seconds REAL")
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


def init():
    global _conn
    _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _migrate()
    _conn.executescript(SCHEMA)
    _conn.commit()


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


def insert_event(level, category, message, detail=None):
    global _event_count
    _exec("INSERT INTO events(ts, level, category, message, detail) VALUES(?,?,?,?,?)",
          (now(), level, category, message, detail))
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

def create_session(bucket, source, label, trigger):
    cur = _exec("INSERT INTO sessions(bucket, source, label, trigger, status, started_at) "
                "VALUES(?,?,?,?,?,?)", (bucket, source, label, trigger, "queued", now()))
    return cur.lastrowid


def update_session(sid, **kw):
    cols = ", ".join(f"{k}=?" for k in kw)
    _exec(f"UPDATE sessions SET {cols} WHERE id=?", (*kw.values(), sid))


def bump_session(sid, done=0, skipped=0, failed=0, bytes_done=0):
    _exec("""UPDATE sessions SET done_files=done_files+?, skipped_files=skipped_files+?,
             failed_files=failed_files+?, done_bytes=done_bytes+? WHERE id=?""",
          (done, skipped, failed, bytes_done, sid))


def get_session(sid):
    return _row("SELECT * FROM sessions WHERE id=?", (sid,))


def list_sessions(limit=50):
    return _rows("SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (limit,))


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


def find_duplicate(bucket, sha256, size, exclude_key):
    """A verified object in the same bucket with identical content."""
    return _row("""SELECT key FROM files WHERE bucket=? AND sha256=? AND size=?
                   AND status='verified' AND key != ? LIMIT 1""",
                (bucket, sha256, size, exclude_key))


def search_files(bucket=None, q=None, status=None, session_id=None, limit=100, offset=0):
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
    if session_id:
        where += " AND session_id = ?"
        params.append(session_id)
    total = _row(f"SELECT COUNT(*) c, COALESCE(SUM(size),0) b FROM files {where}", params)
    items = _rows(f"SELECT * FROM files {where} ORDER BY bucket, key LIMIT ? OFFSET ?",
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


def distinct_buckets():
    return [r["bucket"] for r in _rows(
        "SELECT DISTINCT bucket FROM files ORDER BY bucket")]


def uploading_files(limit=20):
    return _rows("SELECT bucket, key, size, session_id FROM files WHERE status='uploading' "
                 "ORDER BY id LIMIT ?", (limit,))


def fail_stale_uploads():
    """Mark files left in 'uploading' by a crash/restart as failed so the next
    session retries them (and the UI doesn't show ghost uploads)."""
    cur = _exec("UPDATE files SET status='failed', error='interrupted by restart' "
                "WHERE status='uploading'")
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
