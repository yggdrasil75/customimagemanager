"""! @file
@brief The library as data for the library pipeline: one item per file with its
taken time, position, place, people, tags, albums and owner, plus embeddings.

Everything is read in bulk from the DB tables the core and other modules keep
(files, geo / places from map, face_regions from people, image_embeddings from
embedding); a table that isn't there (its module is off) just leaves the field
empty, so the pipeline degrades instead of failing.
"""

import json

import numpy as np

# Taken-date buckets in precedence order. The file-time buckets (d_digitized
# also holds the file creation time, d_modified the mtime) say nothing about
# when a picture was taken, so they only count when asked for.
TAKEN = ("d_original", "d_capture", "d_actual")
FILE_TIMES = ("d_digitized", "d_modified")
CHUNK = 500
KINDS = ("image", "video")


def _list(v):
    """! @brief A JSON list column, a comma list, or nothing -> list of clean names."""
    if not v:
        return []
    if isinstance(v, list):
        out = v
    else:
        try:
            out = json.loads(v)
            if not isinstance(out, list):
                out = [out]
        except (TypeError, ValueError):
            out = str(v).split(",")
    return [str(x).strip().lstrip("?") for x in out if str(x).strip().lstrip("?")]


class Library:
    """! @brief Bulk reads of everything the pipeline groups by. One per run (it caches)."""

    def __init__(self, host, use_file_dates=False):
        self.host = host
        self.db = host.db()
        self.use_file_dates = bool(use_file_dates)
        self._items = {}
        self._tables = {r[0] for r in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        own = host.get_service("ownership")
        self._owner_of = own["owner_of"] if own else (lambda rel: None)

    def has(self, table):
        return table in self._tables

    # -- scope ---------------------------------------------------------------
    def scope(self, q="", folder="", album=""):
        """! @brief rel_paths of the pictures and videos a gallery query lists."""
        where, params, _text, _structured = self.host.core.files_where(q or "", folder or "", album or "",
                                                                         recursive=True)
        extra = "COALESCE(files.media_kind,'image') IN ('image','video')"
        sql = "SELECT files.rel_path FROM files%s%s%s ORDER BY files.rel_path" % (
            where, " AND " if where else " WHERE ", extra)
        return [r[0] for r in self.db.execute(sql, list(params)).fetchall()]

    # -- items ---------------------------------------------------------------
    def items(self, rels):
        """! @brief The item dicts of rel_paths (cached), in the given order."""
        missing = [r for r in rels if r not in self._items]
        for i in range(0, len(missing), CHUNK):
            self._load([r for r in missing[i:i + CHUNK]])
        return [self._items[r] for r in rels if r in self._items]

    def item(self, rel):
        got = self.items([rel])
        return got[0] if got else None

    def _in(self, rels):
        return "(%s)" % ",".join("?" * len(rels))

    def _load(self, rels):
        if not rels:
            return
        buckets = TAKEN + (FILE_TIMES if self.use_file_dates else ())
        cols = ", ".join("%s_epoch" % c for c in buckets)
        rows = self.db.execute(
            "SELECT rel_path, media_kind, rating, tags, albums, persons, %s FROM files WHERE rel_path IN %s"
            % (cols, self._in(rels)), rels).fetchall()
        for r in rows:
            t = next((r["%s_epoch" % c] for c in buckets if r["%s_epoch" % c] is not None), None)
            rel = r["rel_path"]
            self._items[rel] = {
                "rel": rel, "t": t, "lat": None, "lon": None, "place": {},
                "people": _list(r["persons"]), "tags": _list(r["tags"]),
                "kind": r["media_kind"] or "image", "rating": r["rating"],
                "folder": rel.rsplit("/", 1)[0] if "/" in rel else "",
                "albums": _list(r["albums"]), "owner": self._owner_of(rel)}
        found = [r for r in rels if r in self._items]
        if not found:
            return
        q = self._in(found)
        if self.has("geo"):
            for r in self.db.execute("SELECT rel_path, lat, lon FROM geo WHERE lat IS NOT NULL AND rel_path IN " + q, found):
                self._items[r["rel_path"]].update(lat=r["lat"], lon=r["lon"])
        if self.has("places"):
            for r in self.db.execute("SELECT rel_path, city, admin1, country, cc, lat, lon FROM places WHERE rel_path IN " + q, found):
                it = self._items[r["rel_path"]]
                it["place"] = {k: r[k] for k in ("city", "admin1", "country", "cc") if r[k]}
                if it["lat"] is None and r["lat"] is not None:
                    it.update(lat=r["lat"], lon=r["lon"])
        if self.has("face_regions"):
            for r in self.db.execute("SELECT rel_path, name FROM face_regions WHERE name != '' AND rel_path IN " + q, found):
                ppl = self._items[r["rel_path"]]["people"]
                if r["name"] not in ppl:
                    ppl.append(r["name"])

    # -- embeddings ------------------------------------------------------------
    def vectors(self, rels):
        """! @brief {rel: vector} in the space the embedding module searches ({} when off)."""
        svc = self.host.get_service("embedding")
        if not svc or not self.has("image_embeddings") or not rels:
            return {}
        try:
            tag = svc["embedding_model_tag"](self.db)
        except Exception:
            tag = None
        out = {}
        for i in range(0, len(rels), CHUNK):
            chunk = rels[i:i + CHUNK]
            sql = "SELECT rel_path, vec FROM image_embeddings WHERE rel_path IN " + self._in(chunk)
            args = list(chunk)
            if tag:
                sql += " AND model=?"
                args.append(tag)
            for r in self.db.execute(sql, args):
                out[r["rel_path"]] = np.frombuffer(r["vec"], dtype=np.float32)
        return out

    # -- albums -----------------------------------------------------------------
    def album_members(self):
        """! @brief {album: set(rel_paths)} for every album."""
        out = {}
        for r in self.db.execute("SELECT album, rel_path FROM album_members"):
            out.setdefault(r["album"], set()).add(r["rel_path"])
        return out

    def album_names(self):
        return {r[0] for r in self.db.execute("SELECT name FROM albums")}