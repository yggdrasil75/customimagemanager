"""
Metadata sources hub — registry + search/apply for external lookups.
======================================================================
Plex has its own library and falls back to IMDb/TVDB; Calibre asks
Goodreads/Google. This module is that fallback layer for the three media
kinds the app manages: photos, music and books. It owns the registry, the
HTTP helper, the "what do I know about this file" query builder, the
candidate merge and the per-kind writer. It knows nothing about any
particular site: every site is its own module (metasrc_openlibrary,
metasrc_musicbrainz, metasrc_danbooru, …) that registers into the
`metasrc` service this module publishes.

A source is a dict:
    id          stable id ("openlibrary")
    label       human label
    kind        "photo" | "music" | "book"
    priority    optional int; higher sorts first in the candidate list
    available() -> bool             (optional; key configured, etc.)
    search(query) -> [candidate]    query: {"q", "rel_path", "abs_path",
                                    "current": <current fields>, plus kind
                                    hints: isbn/title/authors, artist/album,
                                    md5/lat/lon}
    detail(id) -> fields            (optional; called on apply so search
                                    can stay one cheap request)

A candidate is {"id", "title", "subtitle", "thumb", "fields"}; the hub adds
"source". Fields are the kind's normalised keys:
    photo  tags[], description, artist, source_url
    music  title, artist, album, albumartist, track, disc, year, genre, composer
    book   title, authors[], series, series_index, publisher, published,
           language, isbn, description, subjects[], identifiers{}
"""
import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from flask import request, jsonify

from optional_deps import optional_import

pyexiv2, _HAVE_EXIV = optional_import("pyexiv2", quiet=True)

MANIFEST = {
    "id":          "metasrc",
    "name":        "Metadata sources",
    "version":     "1.0.0",
    "description": "Look up photo / music / book metadata from external sources "
                   "(Open Library, MusicBrainz, Danbooru, …). Source modules "
                   "register into this one.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["metasrc.js"],
}

KINDS = ("photo", "music", "book")
USER_AGENT = "customimagemanager/1.0 (+https://github.com/yggdrasil75/customimagemanager)"
_MUSIC_KEYS = ("title", "artist", "album", "albumartist", "track", "disc", "year", "genre", "composer")
_BOOK_KEYS = ("title", "authors", "series", "series_index", "publisher", "published",
              "language", "isbn", "description", "subjects", "identifiers")


def http_json(url, params=None, headers=None, timeout=12, data=None, method=None):
    """GET (or POST `data`) `url` and parse JSON. Raises on HTTP/network error."""
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    hdrs.update(headers or {})
    if isinstance(data, dict):
        data = json.dumps(data).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "null")


def http_multipart(url, fields, files, headers=None, timeout=30):
    """POST multipart/form-data; files = {name: (filename, bytes)}."""
    b = "----cim" + hashlib.md5(os.urandom(8)).hexdigest()
    body = bytearray()
    for k, v in (fields or {}).items():
        body += (f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    for k, (fn, blob) in (files or {}).items():
        body += (f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fn}\"\r\n"
                 "Content-Type: application/octet-stream\r\n\r\n").encode() + blob + b"\r\n"
    body += f"--{b}--\r\n".encode()
    hdrs = {"User-Agent": USER_AGENT, "Content-Type": f"multipart/form-data; boundary={b}"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=bytes(body), headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "null")


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _gps(path):
    """(lat, lon) from EXIF GPS, or None."""
    if not _HAVE_EXIV:
        return None
    try:
        with pyexiv2.Image(path) as img:
            ex = img.read_exif()
    except Exception:
        return None
    def _deg(s):
        out = 0.0
        for i, part in enumerate(str(s).split()[:3]):
            n, _, d = part.partition("/")
            out += float(n) / float(d or 1) / (60 ** i)
        return out
    try:
        lat = _deg(ex["Exif.GPSInfo.GPSLatitude"]); lon = _deg(ex["Exif.GPSInfo.GPSLongitude"])
    except (KeyError, ValueError, ZeroDivisionError):
        return None
    if ex.get("Exif.GPSInfo.GPSLatitudeRef", "N").startswith("S"):
        lat = -lat
    if ex.get("Exif.GPSInfo.GPSLongitudeRef", "E").startswith("W"):
        lon = -lon
    return lat, lon


def merge_fields(kind, current, fields, overwrite):
    """Fields to actually write: with overwrite=False only fill blanks. Photo
    tags are always unioned (a lookup adds tags, it never drops yours)."""
    out = {}
    for k, v in (fields or {}).items():
        if v in (None, "", [], {}):
            continue
        cur = (current or {}).get(k)
        if kind == "photo" and k == "tags":
            have = list(cur or [])
            out[k] = have + [t for t in v if t not in have]
        elif overwrite or cur in (None, "", [], {}, 0):
            out[k] = v
    return out


class Registry:
    def __init__(self):
        self._sources = {}
        self.http_json = http_json
        self.http_multipart = http_multipart

    def register(self, source):
        assert source["kind"] in KINDS, f"metasrc: bad kind {source.get('kind')!r}"
        self._sources[source["id"]] = source
        return source["id"]

    def get(self, sid):
        return self._sources.get(sid)

    def for_kind(self, kind):
        return sorted((s for s in self._sources.values() if s["kind"] == kind),
                      key=lambda s: -(s.get("priority") or 0))

    def available(self, source):
        try:
            return bool(source["available"]()) if callable(source.get("available")) else True
        except Exception:
            return False

    def listing(self):
        return {k: [{"id": s["id"], "label": s["label"], "available": self.available(s)}
                    for s in self.for_kind(k)] for k in KINDS}


def register(host):
    reg = Registry()
    host.provide_service("metasrc", reg)
    host.add_asset("metasrc.js")
    host.register_feature("metasrc", "Metadata lookup (read=search, write=apply)",
                          section="modules", default="write", role_defaults={"viewer": "read"})
    core = host.core
    log = host.logger

    # ── what we already know about the file (query hints + fill baseline) ──
    def _current(kind, rel):
        if kind == "music":
            r = host.db().execute("SELECT * FROM music WHERE rel_path=?", (rel,)).fetchone()
            return {k: r[k] for k in _MUSIC_KEYS} if r else {}
        if kind == "book":
            r = host.db().execute("SELECT * FROM books WHERE rel_path=?", (rel,)).fetchone()
            if not r:
                return {}
            cur = {k: r[k] for k in _BOOK_KEYS}
            for k in ("authors", "subjects", "identifiers"):
                cur[k] = json.loads(r[k] or ("{}" if k == "identifiers" else "[]"))
            return cur
        m = core.read_metadata(host.safe_path(host.media_dir, rel))
        return {"tags": m.get("tags") or [], "description": m.get("description") or "",
                "artist": m.get("artist") or ""}

    def _query(kind, rel, q):
        ap = host.safe_path(host.media_dir, rel) if rel else None
        cur = _current(kind, rel) if rel else {}
        query = {"rel_path": rel, "abs_path": ap, "current": cur, "q": (q or "").strip()}
        stem = os.path.splitext(os.path.basename(rel or ""))[0]
        if kind == "music":
            query.update(title=cur.get("title") or stem, artist=cur.get("artist") or "",
                         album=cur.get("album") or "")
            query["q"] = query["q"] or " ".join(x for x in (query["artist"], query["title"]) if x)
        elif kind == "book":
            query.update(title=cur.get("title") or stem, authors=cur.get("authors") or [],
                         isbn=(cur.get("isbn") or "").replace("-", ""))
            query["q"] = query["q"] or " ".join([query["title"]] + query["authors"][:1])
        else:
            gps = _gps(ap) if ap and os.path.exists(ap) else None
            query.update(md5=_md5(ap) if ap and os.path.exists(ap) else "",
                         lat=gps[0] if gps else None, lon=gps[1] if gps else None)
            query["q"] = query["q"] or stem
        return query

    # ── writers ───────────────────────────────────────────────────────────
    def _write(kind, rel, fields):
        if kind == "music":
            fn = (host.get_service("music") or {}).get("write_meta")
            if not fn:
                return False, "music module is off"
            return fn(rel, {k: v for k, v in fields.items() if k in _MUSIC_KEYS}), ""
        if kind == "book":
            fn = (host.get_service("books") or {}).get("update_meta")
            if not fn:
                return False, "books module is off"
            return fn(rel, {k: v for k, v in fields.items() if k in _BOOK_KEYS}), ""
        ap = host.safe_path(host.media_dir, rel)
        m = core.read_metadata(ap)
        ok = core.write_metadata(ap, fields.get("tags", m.get("tags") or []),
                                 fields.get("description", m.get("description") or ""),
                                 m.get("regions") or [])
        xmp = {k: v for k, v in (("dc.creator", [fields["artist"]] if fields.get("artist") else None),
                                 ("dc.source", fields.get("source_url"))) if v}
        if ok and xmp:
            w = (host.get_service("xmp") or {}).get("write")
            if w and not w(ap, xmp).get("success"):
                log.warning(f"metasrc: xmp write failed for {rel}")
        return ok, ""

    # ── routes ────────────────────────────────────────────────────────────
    def api_sources():
        return jsonify({"success": True, "sources": reg.listing()})

    def api_search():
        d = request.get_json(force=True, silent=True) or {}
        kind, rel = d.get("kind"), d.get("rel_path") or ""
        if kind not in KINDS:
            return jsonify({"success": False, "error": "bad kind"}), 400
        try:
            query = _query(kind, rel, d.get("q"))
        except Exception as e:
            return jsonify({"success": False, "error": f"could not read file: {e}"}), 400
        srcs = [s for s in reg.for_kind(kind) if reg.available(s)
                and (not d.get("source") or s["id"] == d["source"])]
        def _one(s):
            try:
                out = s["search"](dict(query)) or []
            except Exception as e:
                log.warning(f"metasrc {s['id']}: {e}")
                return [], f"{s['label']}: {e}"
            for c in out:
                c["source"] = s["id"]; c["source_label"] = s["label"]
            return out, ""
        cands, errors = [], []
        # ponytail: one thread per source, no cap — a handful of sources is the realistic max
        with ThreadPoolExecutor(max_workers=max(1, len(srcs))) as ex:
            for out, err in ex.map(_one, srcs):
                cands += out
                if err:
                    errors.append(err)
        return jsonify({"success": True, "query": query["q"], "current": query["current"],
                        "candidates": cands, "errors": errors})

    def api_apply():
        d = request.get_json(force=True, silent=True) or {}
        kind, rel = d.get("kind"), d.get("rel_path") or ""
        if kind not in KINDS or not rel:
            return jsonify({"success": False, "error": "kind and rel_path required"}), 400
        fields = dict(d.get("fields") or {})
        src = reg.get(d.get("source") or "")
        if src and callable(src.get("detail")) and d.get("id") is not None:
            try:
                for k, v in (src["detail"](d["id"]) or {}).items():
                    if v in (None, "", [], {}):
                        continue
                    fields[k] = fields[k] + [x for x in v if x not in fields[k]] \
                        if isinstance(v, list) and isinstance(fields.get(k), list) else v
            except Exception as e:
                log.warning(f"metasrc detail {src['id']}: {e}")
        to_write = merge_fields(kind, _current(kind, rel), fields, bool(d.get("overwrite")))
        if not to_write:
            return jsonify({"success": True, "written": {}, "note": "nothing to fill"})
        ok, err = _write(kind, rel, to_write)
        if not ok:
            return jsonify({"success": False, "error": err or "write failed"}), 500
        core.audit("metasrc_apply", f"kind={kind} file={rel!r} source={d.get('source')!r} "
                                    f"fields={sorted(to_write)}")
        return jsonify({"success": True, "written": to_write})

    host.add_route("/api/metasrc/sources", api_sources, feature="metasrc")
    host.add_route("/api/metasrc/search", api_search, methods=["POST"], feature="metasrc")
    host.add_route("/api/metasrc/apply", api_apply, methods=["POST"], feature="metasrc", level="write")
    host.logger.info("metasrc: registry + routes registered")
