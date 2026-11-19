"""Link duplicates that an upload session skipped before ColdVault recorded
skips — from the "skip (duplicate content)" log lines.

A duplicate is a file whose bytes are already archived under another key, so
it wasn't uploaded again. Linking it records that its own key exists and which
archived object holds its content, so it shows in the Tree and can be restored
and downloaded under its own path.

Sources, both in the same line format:
  - this master's event log (agents forward their lines here), and
  - a file of log lines, e.g. grep'd from an agent's data/coldvault.log*, or
    the "<file>  ->  s3://<bucket>/<key>" pairs that grep + sed produce.

Each line is checked before anything is linked: it must belong to the
session's bucket, fall inside the session's run when it carries a timestamp,
point at an object the index holds as archived with nothing archived under the
skipped file's own key — and the file must really be in the session's source
folder on the machine that uploaded it (asked there), so a line from another
session can't create a path that never existed. Master only.
"""

import os
import re
from datetime import datetime, timedelta

import db
from uploader import make_key

_LOG = re.compile(r"skip \(duplicate content\): (?P<rel>.+?) is identical to "
                  r"already-archived s3://(?P<bucket>[^/\s]+)/(?P<target>.+?)\s*$")
_PAIR = re.compile(r"^\s*(?P<rel>.+?)  ->  s3://(?P<bucket>[^/\s]+)/(?P<target>.+?)\s*$")
_TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
SLACK = timedelta(minutes=2)
SAMPLE = 20


def parse(lines, ts=None):
    """[(timestamp or None, rel, bucket, target)] from log lines; plus the
    number of lines that weren't duplicate-skip lines."""
    out, other = [], 0
    for line in lines:
        line = line.rstrip("\n")
        if not line.strip():
            continue
        m = _LOG.search(line) or _PAIR.match(line)
        if not m:
            other += 1
            continue
        t = ts
        if t is None:
            mt = _TS.match(line)
            t = mt.group(1) if mt else None
        out.append((t, m.group("rel"), m.group("bucket"), m.group("target")))
    return out, other


def _when(ts):
    try:
        return datetime.strptime(ts, db.TIME_FMT)
    except (TypeError, ValueError):
        return None


def from_events(session):
    """The session's duplicate-skip lines still in this master's event log."""
    rows = db.duplicate_skip_events(session.get("location") or "",
                                    session["started_at"], session.get("finished_at"))
    out = []
    for r in rows:
        found, _ = parse([r["message"]], ts=r["ts"])
        out += found
    return out


def link(session, entries, exists, apply=False):
    """Check every entry against the session and the index; with apply=True,
    record the valid ones as links (and as the session's skipped files).
    `exists(paths)` answers, on the machine that has the files, which of the
    given paths exist. Returns counts per outcome and a sample."""
    bucket, label = session["bucket"], session["label"]
    start = _when(session.get("started_at"))
    end = _when(session.get("finished_at"))
    rep = {"lines": len(entries), "linked": 0, "already": 0, "other_bucket": 0,
           "outside_session": 0, "target_missing": 0, "archived_itself": 0,
           "not_on_disk": 0, "sample": []}
    seen, todo = set(), []
    for ts, rel, b, target in entries:
        if b != bucket:
            rep["other_bucket"] += 1
            continue
        t = _when(ts)
        if t and ((start and t < start - SLACK) or (end and t > end + SLACK)):
            rep["outside_session"] += 1
            continue
        key = make_key(label, rel)
        if key in seen:
            continue
        seen.add(key)
        tgt = db.get_file(bucket, target)
        if not tgt or tgt["status"] != "verified":
            rep["target_missing"] += 1
            continue
        own = db.get_file(bucket, key)
        if own and own["status"] == "verified":
            rep["archived_itself"] += 1
            continue
        old = db.get_link(bucket, key)
        if old and old["target_key"] == target:
            rep["already"] += 1
            continue
        todo.append((rel, key, target, tgt))

    # Only files that really are in the session's source folder.
    found = []
    for i in range(0, len(todo), 2000):
        chunk = todo[i:i + 2000]
        found += exists([os.path.join(session["source"], rel) for rel, *_ in chunk])
    for (rel, key, target, tgt), ok in zip(todo, found):
        if not ok:
            rep["not_on_disk"] += 1
            continue
        rep["linked"] += 1
        if len(rep["sample"]) < SAMPLE:
            rep["sample"].append({"key": key, "target_key": target, "size": tgt["size"]})
        if apply:
            db.record_skip(session["id"], bucket, key, tgt["size"], "duplicate", target,
                           tgt["sha256"], None)
    return rep
