"""Parse editorial exchange files (FCP7/Premiere XML, FCPXML, Avid AAF) and
extract the media file names they reference, so the objects can be matched
against the index and batch-restored.

Formats:
- xmeml  (FCP7 / Premiere "Final Cut Pro XML"): <file><pathurl>file://localhost/…
- fcpxml (Final Cut Pro X / Resolve): <asset src=…> / <media-rep src=…>
- aaf    (Avid): binary CFB; SourceMob essence descriptors carry
         NetworkLocator/URLString entries. Parsed with pyaaf2, with a raw
         string-scan fallback for files pyaaf2 cannot read.
"""
import re
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree as ET

from logs import log_event

MEDIA_EXTS = {
    # camera / video
    "mxf", "mov", "mp4", "m4v", "mts", "m2ts", "avi", "mkv", "webm",
    "r3d", "braw", "ari", "arri", "crm", "rmf", "raw", "cine",
    # image sequences / stills
    "dpx", "exr", "dng", "cdng", "tif", "tiff", "jpg", "jpeg", "png", "psd",
    # audio
    "wav", "bwf", "aif", "aiff", "mp3", "flac", "caf",
}


def _is_media_name(name):
    if not name or "." not in name:
        return False
    return name.rsplit(".", 1)[-1].lower() in MEDIA_EXTS


def _norm(ref):
    """URL or path (either slash style) -> clean basename, or None."""
    ref = (ref or "").strip()
    if not ref:
        return None
    if ref.lower().startswith("file:"):
        ref = urlparse(ref).path
    ref = unquote(ref).replace("\\", "/")
    name = ref.rstrip("/").rsplit("/", 1)[-1].strip()
    return name or None


def _parse_xml(path):
    """Handles both xmeml (pathurl elements) and fcpxml (src attributes)."""
    refs = {}
    tree = ET.parse(path)
    for el in tree.iter():
        tag = el.tag.rsplit("}", 1)[-1].lower()
        candidates = []
        if tag == "pathurl" and el.text:          # xmeml
            candidates.append(el.text)
        src = el.get("src")                        # fcpxml asset / media-rep
        if src:
            candidates.append(src)
        # <name>/<filename> as fallback when a file element has no pathurl
        if tag in ("name", "filename") and el.text and _is_media_name(el.text.strip()):
            candidates.append(el.text)
        for c in candidates:
            name = _norm(c)
            if name and _is_media_name(name):
                refs.setdefault(name, c.strip())
    fmt = tree.getroot().tag.rsplit("}", 1)[-1].lower()
    return fmt, refs


def _descriptor_urls(desc):
    """Collect Locator URLStrings from an essence descriptor, recursing into
    multi-descriptor children."""
    urls = []
    if desc is None:
        return urls
    try:
        locators = desc["Locator"].value or []
    except Exception:
        locators = []
    for loc in locators:
        try:
            url = loc["URLString"].value
            if url:
                urls.append(str(url))
        except Exception:
            pass
    try:
        for sub in desc["FileDescriptors"].value or []:
            urls.extend(_descriptor_urls(sub))
    except Exception:
        pass
    return urls


def _parse_aaf_strings(path, refs):
    """Fallback: scan the raw file for printable strings (ascii + utf-16le,
    both common inside CFB containers) that look like media filenames."""
    with open(path, "rb") as fh:
        data = fh.read()
    for m in re.finditer(rb"[ -~]{6,}", data):
        name = _norm(m.group().decode("ascii", "ignore"))
        if name and _is_media_name(name):
            refs.setdefault(name, m.group().decode("ascii", "ignore"))
    for m in re.finditer(b"(?:[ -~]\x00){6,}", data):
        s = m.group().decode("utf-16le", "ignore")
        name = _norm(s)
        if name and _is_media_name(name):
            refs.setdefault(name, s)
    return refs


def _parse_aaf(path):
    refs = {}
    try:
        import aaf2
        with aaf2.open(path, "r") as f:
            for mob in f.content.mobs:
                for url in _descriptor_urls(getattr(mob, "descriptor", None)):
                    name = _norm(url)
                    if name and _is_media_name(name):
                        refs.setdefault(name, url)
                # clip/mob names frequently carry the source filename
                mob_name = (getattr(mob, "name", None) or "").strip()
                if _is_media_name(mob_name):
                    refs.setdefault(_norm(mob_name), mob_name)
        if refs:
            return "aaf", refs
        log_event("WARNING", "editlist",
                  "pyaaf2 found no media locators; falling back to string scan")
    except Exception as e:
        log_event("WARNING", "editlist",
                  f"pyaaf2 could not parse AAF ({e}); falling back to string scan")
    return "aaf (string scan)", _parse_aaf_strings(path, refs)


def parse_edit(path, filename):
    """Returns (format, {basename: original reference string})."""
    if (filename or "").lower().endswith(".aaf"):
        return _parse_aaf(path)
    return _parse_xml(path)
