"""Parse offload/DIT checksum manifests and use their per-file hashes as an
independent integrity check.

Supported formats:
  * CSV  — Silverstack / YoYotta / generic (header-driven, ';' or ',' delimited;
           hash from a "Hash Values" column like "XXH64BE:abcd…" or a named
           XXH64 / MD5 / SHA1 / SHA256 column).
  * MHL  — ASC MHL (.mhl, XML): both v2 (<hash><path>…</path><xxh64>…</xxh64>)
           and older v1.x (<hash><file>…</file><xxhash64be>…). The industry
           standard that camera offload tools write per card.

Files are matched by basename (and name without extension). Each parser returns
a list of records {name, algo, hash, size, aliases}; build_alias_map() turns
those into the alias→(algo,hash,size) map used during upload.
"""
import csv
import hashlib
import os
from xml.etree import ElementTree as ET

from logs import log_event

MANIFEST_ALGOS = ("xxh64", "xxh3", "md5", "sha1", "sha256")

# MHL element tag (localname, lowercased) -> our algorithm name
MHL_HASH_TAGS = {
    "xxh64": "xxh64be", "xxh64be": "xxh64be", "xxh64le": "xxh64le",
    "xxhash64": "xxh64be", "xxhash64be": "xxh64be", "xxhash64le": "xxh64le",
    "xxh3": "xxh3",
    "md5": "md5", "sha1": "sha1", "sha256": "sha256",
}


def xxhash_available():
    try:
        import xxhash  # noqa: F401
        return True
    except ImportError:
        return False


def new_hasher(algo):
    """A hashlib-like object for an algorithm name, or None if unavailable."""
    a = (algo or "").lower()
    if a.startswith("xxh3"):
        try:
            import xxhash
            return xxhash.xxh3_64()
        except Exception:
            return None
    if a.startswith("xxh64") or a == "xxhash64":
        try:
            import xxhash
            return xxhash.xxh64()
        except Exception:
            return None
    if a in ("md5", "sha1", "sha256"):
        return hashlib.new(a)
    return None


def normalize_hash(algo, hexdigest):
    """Our hexdigests are big-endian; manifests tagged …LE are byte-reversed."""
    a = (algo or "").lower()
    if a.endswith("le") and len(hexdigest) == 16:
        return "".join(reversed([hexdigest[i:i + 2] for i in range(0, 16, 2)]))
    return hexdigest


# ---------------------------------------------------------------- CSV

def _sniff_delim(line):
    return ";" if line.count(";") >= line.count(",") else ","


def _parse_hashval(cell):
    if not cell:
        return None, None
    first = cell.replace(",", " ").split()[0]
    if ":" in first:
        algo, val = first.split(":", 1)
        return algo.strip(), val.strip()
    return None, None


def parse_csv(path):
    records = []
    try:
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
            first = f.readline()
            delim = _sniff_delim(first)
            f.seek(0)
            rows = list(csv.reader(f, delimiter=delim, quotechar='"'))
    except OSError:
        return records
    if len(rows) < 2:
        return records

    header = [h.strip().lower() for h in rows[0]]

    def col(*names):
        for n in names:
            if n in header:
                return header.index(n)
        return -1

    ci_name = col("name", "file name", "filename", "clip name", "clip", "file")
    ci_size = col("file size", "size", "bytes")
    ci_res = col("resources", "path", "paths", "files", "file path", "source", "location")
    ci_hashval = col("hash values", "hash value", "hashes", "checksum")
    ci_named, named_algo = -1, None
    for a in MANIFEST_ALGOS:
        if a in header:
            ci_named, named_algo = header.index(a), a
            break
    if ci_hashval < 0 and ci_named < 0:
        return records

    def cell(row, i):
        return row[i].strip() if 0 <= i < len(row) else ""

    for row in rows[1:]:
        if not row:
            continue
        algo, hval = None, None
        if ci_hashval >= 0:
            algo, hval = _parse_hashval(cell(row, ci_hashval))
        if not hval and named_algo:
            algo, hval = named_algo, cell(row, ci_named)
        if not hval:
            continue
        size_s = cell(row, ci_size)
        size = int(size_s) if size_s.isdigit() else None

        basenames, paths = [], []
        nm = cell(row, ci_name)
        if nm:
            basenames.append(os.path.basename(nm.replace("\\", "/")))
        if ci_res >= 0:
            for p in cell(row, ci_res).split(","):
                p = p.strip().replace("\\", "/")
                bn = os.path.basename(p)
                if bn:
                    basenames.append(bn)
                    paths.append(p)
        records.append(_record(basenames, algo, hval, size, paths))
    return [r for r in records if r]


# ---------------------------------------------------------------- MHL

def _lname(el):
    return el.tag.rsplit("}", 1)[-1].lower()


def parse_mhl(path):
    records = []
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return records
    for el in root.iter():
        if _lname(el) != "hash":
            continue
        fpath, size, algo, hval = None, None, None, None
        for child in el:
            ln = _lname(child)
            if ln in ("path", "file"):
                fpath = (child.text or "").strip()
                if child.get("size") and str(child.get("size")).isdigit():
                    size = int(child.get("size"))
            elif ln == "size" and (child.text or "").strip().isdigit():
                size = int(child.text.strip())
            elif ln in MHL_HASH_TAGS and not hval:
                v = (child.text or "").strip()
                if v:
                    algo, hval = MHL_HASH_TAGS[ln], v
        if fpath and hval:
            fpath = fpath.replace("\\", "/")
            records.append(_record([os.path.basename(fpath)], algo, hval, size, [fpath]))
    return [r for r in records if r]


# ---------------------------------------------------------------- shared

def _record(basenames, algo, hval, size, paths=None):
    if not hval or not basenames:
        return None
    # primary display name: prefer a basename that has an extension
    primary = next((b for b in basenames if "." in b), basenames[0])
    aliases = set()
    for b in basenames:
        aliases.add(b.lower())
        aliases.add(os.path.splitext(b)[0].lower())
    aliases.discard("")
    return {"name": primary, "algo": algo.lower(), "hash": hval.lower(),
            "size": size, "aliases": aliases, "paths": paths or []}


def parse_any(path, filename=None):
    """Parse one manifest file. Returns (records, format_label)."""
    ext = os.path.splitext(filename or path)[1].lower()
    if ext == ".mhl":
        return parse_mhl(path), "mhl"
    return parse_csv(path), "csv"


def build_alias_map(records):
    m = {}
    for r in records:
        for a in r["aliases"]:
            m.setdefault(a, (r["algo"], r["hash"], r["size"]))
    return m


def load_manifest_records(root, exts=(".csv", ".mhl")):
    """Find and parse every manifest under root. Returns (records, files_found)."""
    records, found = [], []
    for dirpath, _dirs, filenames in os.walk(root):
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in exts:
                p = os.path.join(dirpath, fn)
                recs, _fmt = parse_any(p, fn)
                if recs:
                    found.append(p)
                    records.extend(recs)
    return records, found


def load_manifests(root, exts=(".csv", ".mhl")):
    """Find and parse every manifest under root. Returns (alias_map, files_found)."""
    records, found = load_manifest_records(root, exts)
    alias_map = build_alias_map(records)
    if found:
        log_event("INFO", "upload",
                  f"loaded {len(found)} checksum manifest(s) under {root}: "
                  f"{len(records)} file hashes "
                  f"({', '.join(os.path.basename(x) for x in found[:5])})")
    return alias_map, found


def lookup(alias_map, filename):
    b = os.path.basename(filename).lower()
    if b in alias_map:
        return alias_map[b]
    return alias_map.get(os.path.splitext(b)[0])
