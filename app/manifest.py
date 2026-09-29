"""Parse offload/DIT checksum manifests (Silverstack, YoYotta, Pomfort, ...) that
ship next to camera media as CSV, and use their per-file hashes as an independent
integrity check: the file on the drive must match what the offload tool recorded.

Manifests vary, so parsing is header-driven and tolerant:
  * delimiter sniffed (';' or ',')
  * the hash + algorithm come from a "Hash Values" column ("XXH64BE:abcd…") or a
    named-algorithm column (XXH64 / MD5 / SHA1 / SHA256)
  * files are matched by basename (and name without extension), collected from the
    Name column and any Resources/Path column, so a row named "B001_F001" whose
    media is "B001_F001.cine" still matches.
"""
import csv
import hashlib
import os

from logs import log_event

MANIFEST_ALGOS = ("xxh64", "md5", "sha1", "sha256")


def new_hasher(algo):
    """Return a hashlib-like object for an algorithm name, or None if we can't."""
    a = (algo or "").lower()
    if a.startswith("xxh64") or a == "xxhash64":
        try:
            import xxhash
            return xxhash.xxh64()
        except ImportError:
            return None
    if a in ("md5", "sha1", "sha256"):
        return hashlib.new(a)
    return None


def normalize_hash(algo, hexdigest):
    """xxh64 manifests may record little-endian ('XXH64LE'); our hexdigest is
    big-endian. Return the comparison form for the manifest's byte order."""
    a = (algo or "").lower()
    if a.endswith("le") and len(hexdigest) == 16:
        return "".join(reversed([hexdigest[i:i + 2] for i in range(0, 16, 2)]))
    return hexdigest


def _sniff_delim(line):
    return ";" if line.count(";") >= line.count(",") else ","


def _parse_hashval(cell):
    """'XXH64BE:4bea…' (optionally several, space/comma separated) -> (algo, hex)."""
    if not cell:
        return None, None
    first = cell.replace(",", " ").split()[0]
    if ":" in first:
        algo, val = first.split(":", 1)
        return algo.strip(), val.strip()
    return None, None


def parse_file(path):
    """Return {alias_lower: (algo, hexhash, size|None)} for one manifest CSV."""
    entries = {}
    try:
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
            first = f.readline()
            delim = _sniff_delim(first)
            f.seek(0)
            rows = list(csv.reader(f, delimiter=delim, quotechar='"'))
    except OSError:
        return entries
    if len(rows) < 2:
        return entries

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
    ci_named = -1
    named_algo = None
    for a in MANIFEST_ALGOS:
        if a in header:
            ci_named, named_algo = header.index(a), a
            break
    if ci_hashval < 0 and ci_named < 0:
        return entries  # no hashes here — not a checksum manifest

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

        aliases = set()
        nm = cell(row, ci_name)
        if nm:
            b = os.path.basename(nm.replace("\\", "/"))
            aliases.add(b.lower())
            aliases.add(os.path.splitext(b)[0].lower())
        if ci_res >= 0:
            for p in cell(row, ci_res).split(","):
                bn = os.path.basename(p.strip().replace("\\", "/"))
                if bn:
                    aliases.add(bn.lower())
                    aliases.add(os.path.splitext(bn)[0].lower())

        for a in aliases:
            if a:
                entries.setdefault(a, (algo.lower(), hval.lower(), size))
    return entries


def load_manifests(root, exts=(".csv",)):
    """Find and parse every manifest under root. Returns (entries, files_found)."""
    entries, found = {}, []
    for dirpath, _dirs, filenames in os.walk(root):
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in exts:
                p = os.path.join(dirpath, fn)
                parsed = parse_file(p)
                if parsed:
                    found.append(p)
                    for k, v in parsed.items():
                        entries.setdefault(k, v)
    if found:
        log_event("INFO", "upload",
                  f"loaded {len(found)} checksum manifest(s) under {root}: "
                  f"{len(entries)} file hashes ({', '.join(os.path.basename(x) for x in found[:5])})")
    return entries, found


def lookup(entries, filename):
    """Find a manifest entry for a file by basename, then name-without-extension."""
    b = os.path.basename(filename).lower()
    if b in entries:
        return entries[b]
    stem = os.path.splitext(b)[0]
    return entries.get(stem)
