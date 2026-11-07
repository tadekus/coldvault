"""A second machine (COLDVAULT_ROLE=agent) — e.g. a Raspberry Pi next to the
master. It does the work that has to happen where its disks are, and nothing
else:

  - watches its own drives for canary files and reports them to the master
  - runs the upload and download sessions the master creates for it, reading
    and writing its own disks and talking to S3 directly
  - answers the master's questions about its files (list a folder, plan a
    download, scan the download folder, …)

It has no database: every record goes to the master (remote_db), and when the
master is unreachable it stops — the file in flight finishes and waits to be
recorded, nothing new starts. Everything is managed from the master's web UI;
this machine serves only a small status page, plus the eject endpoints for
its own drives.
"""

import collections
import shutil
import threading
import time
import uuid

from flask import Flask, jsonify, request

import awsapi
import config
import downloader
import eject
import localfs
import remote_db
import uploader
import version
import watcher
from logs import log_event, outbox
from remote_db import RemoteError

BOOT_ID = uuid.uuid4().hex    # a new one per start, so the master notices restarts
POLL_TIMEOUT = 45             # the master holds a check-in for up to 20 s


def _problem():
    if not config.MASTER_URL:
        return "COLDVAULT_MASTER_URL is not set — e.g. http://master-host:9999"
    if not config.AGENT_TOKEN:
        return "COLDVAULT_AGENT_TOKEN is not set — use the same value as on the master"
    return None


class Agent:
    def __init__(self):
        self.up = uploader.Uploader()
        self.up.on_session_done = self._after_upload
        self.down = downloader.Downloader()
        self.down.on_session_done = lambda sid: self.accepted_dl.discard(sid)
        self.watch = watcher.Watcher(self._drives_changed)
        self._reported = None    # drives as last sent to the master
        self._apply_lock = threading.Lock()   # check-ins can overlap (see _drives_changed)
        self.accepted_up, self.accepted_dl = set(), set()
        self.handling = set()    # request ids being answered right now
        self.answered = collections.deque(maxlen=500)   # recently answered ids
        self.master = {}
        self.last_checkin = None
        self.refused = None      # the master's answer when it turns us away

    # ---- check-ins ----

    def heartbeat(self):
        try:
            free = shutil.disk_usage(config.DOWNLOAD_DIR).free
        except OSError:
            free = None
        return {"boot_id": BOOT_ID, "version": version.VERSION, "drives": self.watch.last,
                "accepted_uploads": sorted(self.accepted_up),
                "accepted_downloads": sorted(self.accepted_dl),
                "handling": sorted(self.handling) + list(self.answered),
                "activity": {"upload_session": self.up.current_session,
                             "upload_queue": self.up.queue_size(),
                             "download_session": self.down.current_session,
                             "download_queue": self.down.queue_size()},
                "download_dir": config.DOWNLOAD_DIR, "download_free": free,
                "roots": config.BROWSE_ROOTS, "eject": eject.listing()["pending"]}

    def run(self):
        delay, down_since = 2, None
        while True:
            try:
                # The first check-in returns at once: the watcher waits for the
                # master's settings (canary name) before it starts.
                body = dict(self.heartbeat(), no_wait=not self.watch.is_alive())
                r = remote_db.request("/api/agent/poll", body, timeout=POLL_TIMEOUT)
            except (ConnectionError, RemoteError) as e:
                if down_since is None:
                    down_since = time.monotonic()
                    log_event("ERROR" if isinstance(e, RemoteError) else "WARNING", "agent",
                              f"can't check in with the master ({e}) — not starting any new "
                              f"work until it answers")
                self.refused = str(e) if isinstance(e, RemoteError) else None
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if down_since is not None:
                log_event("INFO", "agent", f"checked in with the master again after "
                                           f"{int(time.monotonic() - down_since)}s")
                down_since = None
            delay, self.refused = 2, None
            self.last_checkin = remote_db.now()
            self.apply(r)

    def _drives_changed(self, found):
        """A drive appeared or went: tell the master now rather than at the end
        of the current (up to 20 s) check-in."""
        if found == self._reported:
            return
        self._reported = found

        def quick():
            try:
                body = dict(self.heartbeat(), no_wait=True)
                self.apply(remote_db.request("/api/agent/poll", body, timeout=POLL_TIMEOUT))
            except (ConnectionError, RemoteError):
                pass   # the regular check-in will carry it
        threading.Thread(target=quick, daemon=True).start()

    def apply(self, r):
        with self._apply_lock:
            self._apply(r)

    def _apply(self, r):
        p = r.get("policy") or {}
        # Settings that must match the master, so keys built here are the keys
        # the master expects.
        config.PREFIX = p.get("prefix", config.PREFIX)
        config.STORAGE_CLASS = p.get("storage_class", config.STORAGE_CLASS)
        config.DEDUPE = p.get("dedupe", config.DEDUPE)
        config.MANIFEST_CHECK = p.get("manifest_check", config.MANIFEST_CHECK)
        config.MANIFEST_EXTS = tuple(p.get("manifest_exts") or config.MANIFEST_EXTS)
        config.EXCLUDES = list(p.get("excludes") or config.EXCLUDES)
        config.CANARY_NAME = p.get("canary") or config.CANARY_NAME
        awsapi.set_regions(p.get("bucket_regions"))
        self.master = r.get("master") or {}
        if not self.watch.is_alive():   # only once the canary name is the master's
            self.watch.start()

        for sid in r.get("uploads") or []:
            if sid not in self.accepted_up:
                self.accepted_up.add(sid)
                log_event("INFO", "agent", f"picked up upload session #{sid}")
                self.up.run_assigned(sid)
        for sid in r.get("downloads") or []:
            if sid not in self.accepted_dl:
                self.accepted_dl.add(sid)
                log_event("INFO", "agent", f"picked up download session #{sid}")
                self.down.run_assigned(sid)
        for req in r.get("requests") or []:
            rid = req.get("id")
            if rid in self.handling or rid in self.answered:
                continue
            self.handling.add(rid)
            threading.Thread(target=self.answer, args=(req,), daemon=True).start()

    def answer(self, req):
        op, body = req.get("op"), {"id": req.get("id")}
        try:
            fn = localfs.OPS.get(op)
            if not fn:
                raise localfs.FsError(f"this agent doesn't know '{op}' — update it", 501)
            body["result"] = fn(**(req.get("args") or {}))
        except localfs.FsError as e:
            body.update(error=str(e), status=e.status)
        except Exception as e:
            body.update(error=f"{op} failed on {config.NAME}: {e}", status=500)
        try:
            remote_db.request("/api/agent/reply", body, timeout=30)
        except (ConnectionError, RemoteError):
            pass   # the UI that asked has timed out; nothing else to do
        finally:
            self.answered.append(req.get("id"))
            self.handling.discard(req.get("id"))

    def _after_upload(self, sid, trigger):
        self.accepted_up.discard(sid)
        if trigger == "canary":
            eject.after_session(remote_db.get_session(sid), busy=self.up.queue_size() > 0)

    # ---- log lines to the master ----

    def forward_events(self):
        while True:
            time.sleep(2)
            if not outbox:
                continue
            batch = [outbox.popleft() for _ in range(min(500, len(outbox)))]
            try:
                remote_db.request("/api/agent/events", {"events": batch}, timeout=30)
            except (ConnectionError, RemoteError):
                outbox.extendleft(reversed(batch))   # keep order; try again later
                time.sleep(10)


# ---- the agent's own small web page ----

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="5"><title>ColdVault agent</title>
<style>
:root{--bg:#f7f7f8;--fg:#1d1f23;--muted:#6b7078;--card:#fff;--ok:#1f8a4c;--err:#c0392b;--line:#e3e4e8}
@media (prefers-color-scheme: dark){:root{--bg:#0f1115;--fg:#e6e7ea;--muted:#9aa0a9;--card:#171a20;--ok:#3ecf7a;--err:#ff6b5b;--line:#262a33}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:760px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 16px;margin:12px 0}
h1{font-size:18px;margin:8px 0} h2{font-size:14px;margin:0 0 6px}
.k{color:var(--muted);display:inline-block;min-width:150px} code{overflow-wrap:anywhere}
.ok{color:var(--ok)} .err{color:var(--err)} a{color:inherit}
</style></head><body><main>%s</main></body></html>"""


def _esc(s):
    return (str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def make_app(agent, problem):
    app = Flask(__name__)

    @app.get("/")
    def index():
        st = remote_db.status
        if problem:
            conn = f'<span class="err">✘ not configured: {_esc(problem)}</span>'
        elif agent.refused:
            conn = f'<span class="err">✘ refused by the master: {_esc(agent.refused)}</span>'
        elif st["connected"]:
            conn = f'<span class="ok">✔ connected</span> since {_esc(st["since"])}'
        elif st["connected"] is False:
            conn = (f'<span class="err">✘ master unreachable since {_esc(st["since"])} — '
                    f'no new work starts until it is back</span><br><small>{_esc(st["error"])}</small>')
        else:
            conn = "connecting…"
        a = agent.heartbeat()["activity"] if agent else {}
        drives = (agent.watch.last if agent else {}) or {}
        rows = "".join(f'<div><code>{_esc(m)}</code> → {_esc(i["label"])}'
                       f'{" in s3://" + _esc(i["bucket"]) if i.get("bucket") else ""}</div>'
                       for m, i in drives.items()) or '<span class="k">none</span>'
        master = config.MASTER_URL or "(not set)"
        body = f"""<h1>❄ ColdVault agent <code>{_esc(config.NAME)}</code></h1>
<div class="card"><h2>Master</h2>
<div><span class="k">Address</span><a href="{_esc(master)}">{_esc(master)}</a> — manage everything there</div>
<div><span class="k">Connection</span>{conn}</div>
<div><span class="k">Last check-in</span>{_esc(agent.last_checkin if agent else None) or "—"}</div>
<div><span class="k">Versions</span>agent {_esc(version.VERSION)}, master {_esc((agent.master if agent else {}).get("version") or "?")}</div></div>
<div class="card"><h2>Activity</h2>
<div><span class="k">Upload</span>{"session #" + _esc(a.get("upload_session")) if a.get("upload_session") else "idle"}{f", {a.get('upload_queue')} queued" if a.get("upload_queue") else ""}</div>
<div><span class="k">Download</span>{"session #" + _esc(a.get("download_session")) if a.get("download_session") else "idle"}{f", {a.get('download_queue')} queued" if a.get("download_queue") else ""}</div></div>
<div class="card"><h2>Canary drives here</h2>{rows}</div>"""
        return _PAGE % body

    @app.get("/health")
    def health():
        st = remote_db.status
        refused = agent.refused if agent else None
        ok = bool(st["connected"]) and not problem and not refused
        return jsonify({"role": "agent", "name": config.NAME, "version": version.VERSION,
                        "master": config.MASTER_URL, "connected": ok,
                        "since": st["since"], "error": problem or refused or st["error"],
                        "last_checkin": agent.last_checkin if agent else None,
                        "activity": agent.heartbeat()["activity"] if agent else None}), \
            (200 if ok else 503)

    @app.get("/api/eject/pending")
    def eject_pending():
        return jsonify(eject.listing())

    @app.post("/api/eject/done")
    def eject_done():
        data = request.get_json(force=True)
        remaining = eject.done((data.get("mount") or "").strip(), bool(data.get("ok")),
                               (data.get("detail") or "").strip())
        return jsonify({"ok": True, "remaining": remaining})

    return app


def main():
    problem = _problem()
    agent = None
    log_event("INFO", "agent", f"ColdVault {version.VERSION} agent '{config.NAME}' starting — "
                               f"master {config.MASTER_URL or '(not set!)'}")
    if problem:
        log_event("ERROR", "agent", f"not starting: {problem}")
    else:
        agent = Agent()
        threading.Thread(target=agent.run, daemon=True, name="checkin").start()
        threading.Thread(target=agent.forward_events, daemon=True, name="events").start()
    make_app(agent, problem).run(host="0.0.0.0", port=config.PORT, threaded=True)
