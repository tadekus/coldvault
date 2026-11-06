"""Second machines (agents), as the master sees them: who's connected, what
each may do in the database, and getting work and questions to them.

An agent never accepts connections. It checks in with POST /api/agent/poll —
a long poll: the master answers as soon as there is something for it, or after
POLL_WAIT seconds with nothing. That works over Wi-Fi and NAT, and the master
never needs to reach the agent. Work travels in the answer: upload and
download sessions created for that agent, and requests the UI is waiting on
(list a folder, plan a download, scan the download folder, …).

Everything an agent writes goes through db_call(), which allows only the
calls in RPC and only on the agent's own sessions. Every agent request
carries the shared token (COLDVAULT_AGENT_TOKEN); without one configured,
agents are refused outright.
"""

import hmac
import re
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime

import config
import db
import drives
import version
from logs import log_event

PROTOCOL = 1
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
ONLINE_WITHIN = 45     # seconds since the last check-in for "online"
POLL_WAIT = 20         # how long a check-in waits for work before answering empty
FINISHED = ("done", "done_with_errors", "failed")


class AgentError(Exception):
    def __init__(self, message, status=503):
        super().__init__(message)
        self.status = status


def policy():
    """Settings an agent must share with the master, sent at every check-in so
    a key built on the agent is the key the master expects. Performance
    tuning (workers, part sizes) stays each machine's own."""
    return {"prefix": config.PREFIX, "storage_class": config.STORAGE_CLASS,
            "dedupe": config.DEDUPE, "manifest_check": config.MANIFEST_CHECK,
            "manifest_exts": list(config.MANIFEST_EXTS), "excludes": list(config.EXCLUDES),
            "canary": config.CANARY_NAME}


def _age(ts):
    try:
        return (datetime.now() - datetime.strptime(ts, db.TIME_FMT)).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


def _cols(kw, allowed, what):
    extra = set(kw) - set(allowed)
    if extra:
        raise AgentError(f"{what}: field(s) {', '.join(sorted(extra))} not allowed", 403)


class Hub:
    def __init__(self, tracker, on_session_finished):
        self.tracker = tracker
        self.on_session_finished = on_session_finished   # (sid) — uploads only
        self.started = time.monotonic()
        self._lock = threading.Lock()
        self._wake = {}       # name -> Event, set when work or a request arrives
        self._requests = {}   # name -> [{"id", "op", "args"}] not yet answered
        self._gen = {}        # name -> check-in counter; a newer one releases older waits
        self._waiting = {}    # request id -> {"event", "reply"}
        self._present = set() # agents seen by this process (for connect/offline logging)
        self._answers = OrderedDict()   # (name, call id) -> result, for retried calls
        threading.Thread(target=self._reaper, daemon=True, name="agent-reaper").start()

    # ---- identity ----

    def authenticate(self, request):
        """The calling agent's name, or AgentError."""
        if not config.AGENT_TOKEN:
            raise AgentError("agents are disabled on this master — set COLDVAULT_AGENT_TOKEN", 403)
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not hmac.compare_digest(token.encode(), config.AGENT_TOKEN.encode()):
            raise AgentError("wrong agent token", 401)
        name = request.headers.get("X-ColdVault-Agent", "")
        if not NAME_RE.match(name):
            raise AgentError("agent name must be 1–40 letters, digits, '.', '_' or '-' "
                             "(set COLDVAULT_NAME on the agent)", 400)
        if name == config.NAME:
            raise AgentError(f"agent name '{name}' is the master's own name — give the "
                             f"agent a different COLDVAULT_NAME", 400)
        proto = request.headers.get("X-ColdVault-Protocol", "")
        if proto != str(PROTOCOL):
            raise AgentError(f"agent speaks protocol {proto or '?'}, this master "
                             f"{PROTOCOL} — run the same ColdVault version on both", 409)
        return name

    def _event(self, name):
        with self._lock:
            return self._wake.setdefault(name, threading.Event())

    def wake(self, name):
        self._event(name).set()

    def online(self, name):
        r = next((a for a in db.list_agents() if a["name"] == name), None)
        return bool(r) and _age(r["last_seen"]) <= ONLINE_WITHIN

    # ---- check-in ----

    def poll(self, name, body):
        boot = body.get("boot_id") or ""
        info = {k: body.get(k) for k in ("drives", "activity", "download_dir",
                                         "download_free", "roots", "eject")}
        prev = db.touch_agent(name, boot, body.get("version"), info)
        if prev is not None and prev != boot:
            sids, dsids = db.fail_location_work(name, "agent restarted")
            self.tracker.restarted(name)
            if sids or dsids:
                log_event("WARNING", "agent",
                          f"{name} restarted — marked its interrupted upload session(s) "
                          f"{sids or '-'} and download session(s) {dsids or '-'} failed; "
                          f"re-run them to continue (verified files are skipped)")
        with self._lock:
            new = name not in self._present
            self._present.add(name)
        if new:
            log_event("INFO", "agent", f"{name} connected (ColdVault {body.get('version')})"
                      + ("" if body.get("version") == version.VERSION
                         else f" — master runs {version.VERSION}; update both to match"))
        # None = the agent hasn't scanned yet. Treating that as "no drives" would
        # spend the first look on an empty list, and a drive left plugged in
        # across a restart would then be uploaded again.
        if body.get("drives") is not None:
            self.tracker.update(name, body["drives"])

        taken = (set(body.get("accepted_uploads") or []),
                 set(body.get("accepted_downloads") or []),
                 set(body.get("handling") or []))
        ev = self._event(name)
        with self._lock:
            gen = self._gen[name] = self._gen.get(name, 0) + 1
        ev.set()   # release an older check-in still held (e.g. from before an agent restart)
        deadline = time.monotonic() + (0 if body.get("no_wait") else POLL_WAIT)
        while True:
            ev.clear()
            work = self._work(name, taken)
            if any(work.values()) or self._gen.get(name) != gen:
                break
            left = deadline - time.monotonic()
            if left <= 0:
                break
            ev.wait(left)
        return {"policy": policy(),
                "master": {"name": config.NAME, "version": version.VERSION},
                **work}

    def _work(self, name, taken):
        """What `name` hasn't taken on yet. Requests stay listed until answered
        (not just until sent), so one handed to a check-in that never reached
        the agent — a dead connection — is simply handed out again."""
        with self._lock:
            reqs = [r for r in self._requests.get(name, []) if r["id"] not in taken[2]]
        return {"uploads": [s for s in db.queued_sessions(name) if s not in taken[0]],
                "downloads": [s for s in db.queued_dl_sessions(name) if s not in taken[1]],
                "requests": reqs}

    # ---- asking an agent something (the UI waits) ----

    def call(self, name, op, args, timeout=30):
        if not any(a["name"] == name for a in db.list_agents()):
            raise AgentError(f"no agent named '{name}' has ever checked in", 404)
        if not self.online(name):
            raise AgentError(f"{name} is offline — it hasn't checked in for over "
                             f"{ONLINE_WITHIN}s", 503)
        rid = uuid.uuid4().hex
        slot = {"event": threading.Event(), "reply": None}
        with self._lock:
            self._waiting[rid] = slot
            self._requests.setdefault(name, []).append({"id": rid, "op": op, "args": args})
        self.wake(name)
        try:
            if not slot["event"].wait(timeout):
                raise AgentError(f"{name} didn't answer within {timeout}s", 504)
        finally:
            with self._lock:
                self._waiting.pop(rid, None)
                q = self._requests.get(name)
                if q:
                    self._requests[name] = [r for r in q if r["id"] != rid]
        reply = slot["reply"] or {}
        if "error" in reply:
            raise AgentError(reply["error"], reply.get("status") or 400)
        return reply.get("result")

    def reply(self, name, body):
        with self._lock:
            slot = self._waiting.get(body.get("id"))
        if slot:
            slot["reply"] = body
            slot["event"].set()

    # ---- log lines from an agent ----

    def events(self, name, items):
        for e in (items or [])[:2000]:
            db.insert_event(e.get("level") or "INFO", e.get("category") or "agent",
                            f"[{name}] {e.get('message') or ''}", e.get("detail"),
                            ts=e.get("ts"))

    # ---- the agent's database calls ----

    def db_call(self, name, body):
        fn, args, kw = body.get("fn"), list(body.get("args") or []), body.get("kwargs") or {}
        key = (name, body.get("id"))
        with self._lock:
            if body.get("id") and key in self._answers:
                return self._answers[key]
        handler = self.RPC.get(fn)
        if not handler:
            raise AgentError(f"call '{fn}' is not available to agents", 403)
        result = handler(self, name, *args, **kw)
        if isinstance(result, set):
            result = sorted(result)
        with self._lock:
            if body.get("id"):
                self._answers[key] = result
                while len(self._answers) > 5000:
                    self._answers.popitem(last=False)
        return result

    def _own(self, lookup, ident, name, what):
        if lookup(ident) != name:
            raise AgentError(f"{what} {ident} doesn't belong to {name}", 403)

    def _get_session(self, name, sid):
        self._own(db.session_location, sid, name, "upload session")
        return db.get_session(sid)

    def _update_session(self, name, sid, **kw):
        self._own(db.session_location, sid, name, "upload session")
        _cols(kw, ("status", "total_files", "total_bytes", "finished_at"), "update_session")
        db.update_session(sid, **kw)
        if kw.get("status") in FINISHED:
            try:
                self.on_session_finished(sid)
            except Exception as e:
                log_event("ERROR", "agent", f"after-session step failed for #{sid}: {e}")

    def _bump_session(self, name, sid, **kw):
        self._own(db.session_location, sid, name, "upload session")
        db.bump_session(sid, **kw)

    def _claim(self, name, bucket, key, sid, sha256=None, **fields):
        self._own(db.session_location, sid, name, "upload session")
        _cols(fields, ("size", "local_path", "mtime", "storage_class"), "claim_upload")
        return db.claim_upload(bucket, key, sid, sha256=sha256, **fields)

    def _upsert_file(self, name, bucket, key, **kw):
        """Only what the uploader legitimately does: finish (or fail) an upload
        this agent's session reserved, note a new mtime on unchanged content,
        or record a failure for a key nothing has archived yet."""
        _cols(kw, ("mtime", "sha256", "checksum_s3", "etag", "status", "error", "uploaded_at",
                   "verified_at", "upload_seconds", "manifest_state", "manifest_algo",
                   "manifest_hash"), "upsert_file")
        row = db.get_file(bucket, key)
        held = row and row["session_id"] and db.session_location(row["session_id"]) == name
        if not (held or set(kw) == {"mtime"} or (not row and kw.get("status") == "failed")):
            raise AgentError(f"s3://{bucket}/{key} isn't reserved by one of {name}'s sessions", 403)
        return db.upsert_file(bucket, key, **kw)

    def _get_dl_session(self, name, sid):
        self._own(db.dl_session_location, sid, name, "download session")
        return db.get_dl_session(sid)

    def _update_dl_session(self, name, sid, **kw):
        self._own(db.dl_session_location, sid, name, "download session")
        _cols(kw, ("status", "finished_at", "total_files", "total_bytes"), "update_dl_session")
        db.update_dl_session(sid, **kw)

    def _bump_dl_session(self, name, sid, **kw):
        self._own(db.dl_session_location, sid, name, "download session")
        db.bump_dl_session(sid, **kw)

    def _add_download(self, name, sid, bucket, key, local_path, size):
        self._own(db.dl_session_location, sid, name, "download session")
        return db.add_download(sid, bucket, key, local_path, size)

    def _update_download(self, name, did, **kw):
        self._own(db.download_location, did, name, "download")
        _cols(kw, ("status", "error", "sha256", "download_seconds", "finished_at",
                   "manifest_state"), "update_download")
        db.update_download(did, **kw)

    RPC = {
        "get_session": _get_session,
        "update_session": _update_session,
        "bump_session": _bump_session,
        "claim_upload": _claim,
        "upsert_file": _upsert_file,
        "get_file": lambda self, name, bucket, key: db.get_file(bucket, key),
        "find_duplicate": lambda self, name, bucket, sha, size, exclude_key:
            db.find_duplicate(bucket, sha, size, exclude_key),
        "size_taken": lambda self, name, bucket, size, exclude_key:
            db.size_taken(bucket, size, exclude_key),
        "find_keys_any_bucket": lambda self, name, keys: db.find_keys_any_bucket(keys),
        "get_dl_session": _get_dl_session,
        "update_dl_session": _update_dl_session,
        "bump_dl_session": _bump_dl_session,
        "add_download": _add_download,
        "update_download": _update_download,
        # Scoped to the calling agent's own downloads, whatever it asks for.
        "downloaded_paths": lambda self, name, *a, **k: db.downloaded_paths(name),
        "downloaded_rows_to_check": lambda self, name, *a, **k: db.downloaded_rows_to_check(name),
    }

    # ---- for the UI ----

    def listing(self):
        out = []
        for a in db.list_agents():
            age = _age(a["last_seen"])
            out.append({"name": a["name"], "version": a["version"],
                        "online": age <= ONLINE_WITHIN, "last_seen": a["last_seen"],
                        "seconds_ago": None if age == float("inf") else int(age),
                        "first_seen": a["first_seen"], "info": a["info"],
                        "drives": self.tracker.drives(a["name"]),
                        "auto_upload": drives.auto_upload(a["name"]),
                        "work": db.location_activity(a["name"])})
        return out

    def forget(self, name):
        if self.online(name):
            raise AgentError(f"{name} is online — stop it first, then forget it", 409)
        with self._lock:
            self._present.discard(name)
        self.tracker.restarted(name)
        return db.forget_agent(name)

    # ---- an agent that went quiet ----

    def _reaper(self):
        """Log agents going offline; after COLDVAULT_AGENT_FAIL_MINUTES, fail
        what they had in flight so its files aren't reserved forever. Ages
        count from this master's start at the earliest — an agent can't check
        in while the master is down."""
        warned = set()
        while True:
            time.sleep(30)
            try:
                up_for = time.monotonic() - self.started
                for a in db.list_agents():
                    name, age = a["name"], min(_age(a["last_seen"]), up_for)
                    if age <= ONLINE_WITHIN:
                        warned.discard(name)
                        continue
                    with self._lock:
                        was_here = name in self._present
                        self._present.discard(name)
                    if was_here:
                        log_event("WARNING", "agent", f"{name} went offline (last check-in "
                                                      f"{a['last_seen']})")
                    if age > config.AGENT_FAIL_AFTER and name not in warned:
                        sids, dsids = db.fail_location_work(
                            name, f"agent offline for over {config.AGENT_FAIL_AFTER // 60} min")
                        warned.add(name)
                        if sids or dsids:
                            log_event("WARNING", "agent",
                                      f"{name} offline for over {config.AGENT_FAIL_AFTER // 60} "
                                      f"min — marked upload session(s) {sids or '-'} and "
                                      f"download session(s) {dsids or '-'} failed")
            except Exception as e:
                log_event("ERROR", "agent", f"agent reaper error: {e}")
