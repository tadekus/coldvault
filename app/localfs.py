"""The filesystem side of the Upload and Downloads tabs, written once and run
wherever the files are: on this machine, or — asked through the agent hub — on
an agent. Every operation takes and returns plain JSON-able data and raises
FsError(message, status) for anything the user should see.
"""

import os

import config
import downloader
import uploader
import watcher
from logs import log_event


class FsError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _under(path, root):
    real, root = os.path.realpath(path), os.path.realpath(root)
    return real == root or real.startswith(root.rstrip("/") + "/")


def allowed_path(path):
    return any(_under(path, r) for r in config.BROWSE_ROOTS)


def allowed_dest(path):
    return _under(path, config.DOWNLOAD_DIR)


# ---- upload side ----

def browse_roots():
    """The configured upload roots (watch dirs + COLDVAULT_BROWSE_ROOTS), each
    checked for whether it exists and is readable here. Deduplicated, order
    preserved."""
    seen, roots = set(), []
    for r in config.BROWSE_ROOTS:
        real = os.path.realpath(r)
        if real in seen:
            continue
        seen.add(real)
        exists = os.path.isdir(r)
        roots.append({"path": r, "exists": exists,
                      "readable": exists and os.access(r, os.R_OK | os.X_OK),
                      "is_watch": r in config.WATCH_DIRS})
    return {"roots": roots}


def browse(path=None):
    path = path or (config.BROWSE_ROOTS[0] if config.BROWSE_ROOTS else "/media")
    if not allowed_path(path):
        raise FsError(f"path outside allowed roots ({', '.join(config.BROWSE_ROOTS)})", 403)
    if not os.path.isdir(path):
        raise FsError("not a directory")
    dirs, files, total_bytes, nfiles = [], [], 0, 0
    try:
        for e in sorted(os.scandir(path), key=lambda x: x.name.lower()):
            if e.is_dir(follow_symlinks=False):
                dirs.append(e.name)
            elif e.is_file(follow_symlinks=False):
                nfiles += 1
                try:
                    sz = e.stat(follow_symlinks=False).st_size
                except OSError:
                    sz = 0
                total_bytes += sz
                if len(files) < 500:
                    files.append({"name": e.name, "size": sz})
    except OSError as e:
        raise FsError(str(e))
    # A canary in this folder already says where the drive belongs, so a manual
    # upload of it can start from the same label and bucket the watcher would use.
    canary = None
    cpath = os.path.join(path, config.CANARY_NAME)
    if os.path.isfile(cpath):
        canary = watcher.read_canary(cpath, path)
    parent = os.path.dirname(path.rstrip("/"))
    return {"path": path, "parent": parent if allowed_path(parent) else None,
            "dirs": dirs, "files": files, "file_count": nfiles,
            "files_truncated": nfiles > len(files), "total_bytes": total_bytes,
            "canary": canary,
            # names an upload leaves out (COLDVAULT_EXCLUDES, the canary file)
            "excluded": [n for n in dirs + [f["name"] for f in files] if uploader._left_out(n)]}


def prepare_upload(path=None, items=None):
    """Check an upload request against this machine's filesystem. Returns
    {"source": folder, "items": selection|None}: for a selection, nested picks
    are dropped and `source` is their common parent, so keys are made relative
    to it (selecting a folder == uploading it whole)."""
    if items:
        clean = []
        for it in items:
            it = (it or "").strip()
            if not it or not allowed_path(it):
                raise FsError(f"item outside allowed roots: {it}")
            if not os.path.exists(it):
                raise FsError(f"no longer exists: {it}")
            clean.append(os.path.realpath(it))
        dirs = [i for i in clean if os.path.isdir(i)]
        pruned = sorted({i for i in clean
                         if not any(i != d and i.startswith(d + os.sep) for d in dirs)})
        base = os.path.commonpath(pruned) if len(pruned) > 1 else pruned[0]
        if not os.path.isdir(base):
            base = os.path.dirname(base)
        return {"source": base, "items": pruned}
    path = (path or "").strip()
    if not path or not allowed_path(path):
        raise FsError(f"path must be inside: {', '.join(config.BROWSE_ROOTS)}")
    if not os.path.isdir(path):
        raise FsError("not a directory")
    return {"source": path, "items": None}


PLAN_LIMIT = 200_000   # files per preview; a bigger selection is previewed in parts


def plan_upload(path=None, items=None, summary=False):
    """What an upload of this selection would read, found by the uploader's own
    scan — for the preview. Paths are relative to the session's source folder,
    exactly as the uploader makes them into S3 keys. With `summary`, only the
    totals (no file list, no size limit) — for Start upload's confirmation."""
    plan = prepare_upload(path, items)
    src, skipped = plan["source"], []
    files = (uploader.scan_items(plan["items"], skipped) if plan["items"]
             else uploader.scan(src, skipped))
    if summary:
        return {"source": src, "count": len(files), "bytes": sum(f[1] for f in files),
                "excluded_count": len(skipped)}
    if len(files) > PLAN_LIMIT:
        raise FsError(f"{len(files):,} files are too many to preview at once "
                      f"(limit {PLAN_LIMIT:,}) — preview a smaller selection", 413)

    def rel(p):
        r = os.path.relpath(p.rstrip("/"), src).replace(os.sep, "/")
        return r + "/" if p.endswith("/") else r

    return {"source": src, "items": plan["items"],
            "files": [[rel(p), size, mtime] for p, size, mtime in files],
            "excluded": [rel(p) for p in skipped[:500]], "excluded_count": len(skipped)}


# ---- download side ----

def download_browse(path=None):
    path = path or config.DOWNLOAD_DIR
    if not allowed_dest(path):
        raise FsError(f"path must be inside {config.DOWNLOAD_DIR}", 403)
    if not os.path.isdir(path):
        raise FsError("not a directory")
    try:
        dirs = sorted(e.name for e in os.scandir(path) if e.is_dir(follow_symlinks=False))
    except OSError as e:
        raise FsError(str(e))
    parent = os.path.dirname(path.rstrip("/"))
    return {"path": path, "parent": parent if allowed_dest(parent) else None,
            "dirs": dirs, "download_dir": config.DOWNLOAD_DIR}


def plan_download(dest=None, items=(), policy=None):
    """Check a download request against this machine's disk before it is
    queued. Returns {"go": True, "items": […], …} with what to fetch,
    {"needs_confirmation": True, …} when it won't all fit and the user must
    choose, or {"cancelled": True}."""
    dest = (dest or config.DOWNLOAD_DIR).strip()
    items = list(items or [])
    if not items:
        raise FsError("no objects selected")
    if not allowed_dest(dest):
        raise FsError(f"destination must be inside {config.DOWNLOAD_DIR}")
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError as e:
        raise FsError(f"cannot create destination: {e}")
    if not os.access(dest, os.W_OK):
        raise FsError(f"destination not writable: {dest} — check the volume mount is "
                      f"read-write")
    if policy == "cancel":
        return {"cancelled": True}

    # Disk-space pre-flight. Files already present at the target with a matching
    # size will be skipped by the downloader, so they don't count.
    def needs_download(it):
        try:
            target = downloader.safe_dest(dest, it["key"])
            return not (os.path.exists(target) and int(it.get("size") or 0)
                        and os.path.getsize(target) == int(it["size"]))
        except Exception:
            return True

    st = os.statvfs(dest)
    free = st.f_bavail * st.f_frsize
    budget = free - config.DOWNLOAD_MIN_FREE
    to_fetch = [i for i in items if needs_download(i)]
    required = sum(int(i.get("size") or 0) for i in to_fetch)
    if not to_fetch or required <= budget:
        return {"go": True, "dest": dest, "items": items}

    fit, skipped, used = downloader.plan_within_budget(to_fetch, budget)
    if policy == "fit":
        if not fit:
            raise FsError("not enough free space for even one selected file", 507)
        log_event("WARNING", "download",
                  f"insufficient space at {dest} (free {free:,}, need {required:,}): "
                  f"downloading {len(fit)} file(s) that fit, skipped {len(skipped)}")
        return {"go": True, "dest": dest, "items": fit, "partial": True,
                "downloaded_count": len(fit), "skipped_count": len(skipped),
                "skipped": [i.get("key") for i in skipped][:200]}
    return {"needs_confirmation": True, "dest": dest, "free": free, "required": required,
            "reserve": config.DOWNLOAD_MIN_FREE, "fits_count": len(fit), "fits_bytes": used,
            "total_count": len(items), "skipped_count": len(skipped)}


def paths_exist(paths=()):
    return [bool(p) and os.path.exists(p) for p in paths]


def verify_downloads():
    checked, deleted = downloader.verify_local_downloads()
    return {"checked": checked, "deleted": deleted}


def orphans():
    items, totals = downloader.scan_orphans()
    log_event("INFO", "download",
              f"orphan scan of {totals['root']}: {totals['orphans']} untracked file(s), "
              f"{totals['orphan_bytes']:,} bytes ({totals['unarchived']} not in any "
              f"indexed bucket); {totals['tracked']} tracked")
    return {"items": items, **totals}


def delete_orphans(paths=(), force=False):
    """Delete local files from the download folder; S3 is never touched.
    Refuses a file that isn't archived in an indexed bucket — that copy may be
    the only one left — unless `force`."""
    paths = list(paths or [])
    if not paths:
        raise FsError("nothing selected")
    if not force:
        known = {f["path"]: f for f in downloader.scan_orphans()[0]}
        risky = [p for p in paths if not (known.get(p) or {}).get("archived_in")]
        if risky:
            raise FsError(f"{len(risky)} of these are not archived in any indexed bucket — "
                          f"the local copy may be the only one. Re-send with force to "
                          f"delete anyway.", 409)
    deleted, freed, failed = downloader.delete_local(paths)
    log_event("WARNING" if failed else "INFO", "download",
              f"deleted {deleted} local file(s), freed {freed:,} bytes"
              + (f"; {len(failed)} failed" if failed else ""))
    return {"deleted": deleted, "freed": freed, "failed": failed}


# What a master may ask an agent to do (and what the master does for itself).
OPS = {
    "browse_roots": browse_roots,
    "browse": browse,
    "prepare_upload": prepare_upload,
    "plan_upload": plan_upload,
    "download_browse": download_browse,
    "plan_download": plan_download,
    "paths_exist": paths_exist,
    "verify_downloads": verify_downloads,
    "orphans": orphans,
    "delete_orphans": delete_orphans,
}
