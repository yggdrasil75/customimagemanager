"""gallery-dl integration — uses the gallery-dl *library* directly.

gallery-dl is a Python package, so we import it and drive its Job classes
in-process rather than shelling out to a `gallery-dl` binary and parsing stdout.
That means no dependency on a console script being on PATH (a `pip install
gallery-dl` gives you the importable module regardless), no subprocess, and no
JSON-over-stdout round-trip.

Two jobs, nothing more:

  discover_fields(url) -> {"site": str, "fields": [str, ...]}
        Runs a DataJob (no download) with resolve enabled so a booru search or
        user page descends into real per-post metadata, and returns the flat,
        sorted union of metadata keys plus the extractor category. This is what
        the settings UI shows the first time you add a site: pick which field
        feeds `tags` vs `description`.

  download(url, dest) -> yields (media_path, metadata_dict)
        Runs a DownloadJob into `dest` with a JSON-metadata postprocessor, then
        yields each downloaded media file paired with its parsed sidecar. The
        caller maps that onto the library's tags/description and hands the file
        to the normal ingest path.

Config isolation: gallery-dl's config is process-global module state. Before
each call we clear it and load nothing, so the user's own
~/.config/gallery-dl/config.json can't narrow the metadata we collect (a
`metadata.include`/`fields`/`exclude` there would silently trim fields and break
discovery). Anything a site genuinely needs to auth (API key, cookies) is passed
explicitly by the caller via `opts` — a list of "path.key=value" strings applied
for the duration of the call and then restored. Because that config is global, a
module lock serialises gdl calls so concurrent requests don't clobber each
other's settings.
"""
import json
import logging
import os
import shutil
import tempfile
import threading

import gallery_dl.config as _gconfig
from gallery_dl import exception as _gexc
from gallery_dl.extractor import find as _find_extractor
from gallery_dl.job import DataJob, DownloadJob

_log = logging.getLogger("gdl")

# Media extensions gallery-dl may drop next to its .json sidecars. Anything not
# in here (its own .json, .txt notes) is ignored when pairing files to metadata.
_MEDIA_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".webm", ".mp4", ".mkv", ".avif",
    ".jxl", ".bmp", ".tiff", ".tif", ".mov", ".m4v", ".apng", ".svg",
}

# gallery-dl config is global module state; serialise access so concurrent
# requests don't stomp each other's per-call options.
# ponytail: one global lock. Fine — gdl calls are network-bound and rare; swap
# for a config-context-per-thread only if this ever becomes a throughput wall.
_lock = threading.RLock()


class GdlError(RuntimeError):
    pass


def available():
    """True if the gallery-dl library is importable (it is, if this module
    imported at all — kept as a function so callers/UI have a clean check)."""
    return True


def _opts_to_kvlist(opts):
    """Turn ["extractor.danbooru.username=me", ...] into the (path, key, value)
    tuples config.apply() wants. A bare "key=value" targets the ("extractor",)
    section; a dotted "a.b.key=value" nests under ("a","b")."""
    kvlist = []
    for item in opts or []:
        if not item or "=" not in item:
            continue
        dotted, value = item.split("=", 1)
        parts = dotted.split(".")
        key = parts[-1]
        path = tuple(parts[:-1]) or ("extractor",)
        # best-effort JSON coercion so "true"/"123"/'["a"]' aren't left as str
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            pass
        kvlist.append((path, key, value))
    return kvlist


def _fresh_config(opts):
    """Clear any loaded user config and return the scoped-options context
    manager to apply `opts` for the call. Caller uses it as a `with` block."""
    _gconfig.clear()
    return _gconfig.apply(_DEFAULT_INCLUDES + _opts_to_kvlist(opts))


# Ask extractors that gate extra metadata behind an "includes" list to hand it
# over — most importantly `notes` (translation/annotation boxes on e621,
# danbooru, gelbooru, …), which is what the "regions" mapping target consumes.
# Without this the field never appears in discovery and is never populated at
# fetch, so a regions mapping would silently produce nothing. Set before user
# opts so a site that names a different include, or a user who wants to turn it
# off, can override it.
_DEFAULT_INCLUDES = [
    (("extractor",), "metadata", "notes,pools,tags"),
]


def _flatten(obj, prefix=""):
    """Flatten nested dicts into dotted keys, so a booru's
    `{"tags": [...], "user": {"name": ...}}` surfaces both `tags` and
    `user.name` as selectable fields. Lists and scalars are leaves."""
    out = {}
    for k, v in obj.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


def discover_fields(url, opts=None, resolve=2):
    """Return the metadata fields available for `url` as
    {"site": category, "fields": sorted([...])}.

    Uses a DataJob with `resolve` so a queue-style URL (search/user page)
    descends into actual per-post metadata instead of stopping at the parent —
    the fields returned are the real ones a download would expose. `opts` passes
    site credentials explicitly (see module docstring)."""
    if not _find_extractor(url):
        raise GdlError(f"No gallery-dl extractor matches that URL: {url}")
    with _lock, _fresh_config(opts):
        job = DataJob(url, file=None, resolve=resolve)
        job.run()
        meta_dicts = list(job.data_meta)

    fields, site = set(), ""
    for kw in meta_dicts:
        if not isinstance(kw, dict):
            continue
        flat = _flatten(kw)
        fields.update(flat.keys())
        site = site or flat.get("category") or flat.get("subcategory") or ""

    if not fields:
        # data_meta empty usually means the extractor errored before yielding.
        msg = str(err) if err else "gallery-dl found no metadata for that URL."
        raise GdlError(msg)
    return {"site": site, "fields": sorted(fields)}


def site_of(url, opts=None):
    """The extractor category for a URL (e.g. 'danbooru'), from the extractor
    class itself — no network needed."""
    extr = _find_extractor(url)
    return getattr(extr, "category", "") if extr else ""


def download(url, dest, opts=None, on_file=None):
    os.makedirs(dest, exist_ok=True)
    if not _find_extractor(url):
        raise GdlError(f"No gallery-dl extractor matches that URL: {url}")

    # Force our output dir, a flat layout (no per-site subfolders), and a
    # full-metadata JSON sidecar per file.
    pin = [
        (("extractor",), "base-directory", dest),
        (("extractor",), "directory", []),
        (("extractor",), "postprocessors",
         [{"name": "metadata", "mode": "json"}]),
    ]

    # Run the (synchronous, blocking) DownloadJob on a background thread and
    # watch `dest` for finished files from this one. gallery-dl writes each
    # media file and its .json sidecar as it goes, so a media file whose sidecar
    # already exists is complete and safe to hand off — no gallery-dl internals
    # or per-version hook APIs involved, just the filesystem it's producing.
    err_box = {}

    def _run_job():
        try:
            job = DownloadJob(url)
            job.run()
        except Exception as e:                        # surfaced after join
            err_box["err"] = e

    with _lock:
        _gconfig.clear()
        with _gconfig.apply(_DEFAULT_INCLUDES + pin + _opts_to_kvlist(opts)):
            worker = threading.Thread(target=_run_job, name="gdl-job", daemon=True)
            worker.start()

            seen = set()
            # Poll while the job runs, yielding each file once its sidecar lands.
            while worker.is_alive():
                for mpath, meta in _ready_files(dest, seen):
                    if on_file:
                        try:
                            on_file(mpath, meta)
                        except Exception as e:
                            _log.warning("gdl on_file failed for %s: %s", mpath, e)
                    yield mpath, meta
                worker.join(timeout=0.5)
            # Final sweep: catch the last file(s) finished between polls, and any
            # media whose sidecar never appeared (fall back to the file itself).
            for mpath, meta in _ready_files(dest, seen, final=True):
                if on_file:
                    try:
                        on_file(mpath, meta)
                    except Exception as e:
                        _log.warning("gdl on_file failed for %s: %s", mpath, e)
                yield mpath, meta

    err = err_box.get("err")
    if not seen:
        raise GdlError(str(err) if err else "gallery-dl downloaded nothing.")


def _ready_files(dest, seen, final=False):
    """Yield (media_path, metadata) for media files in `dest` not yet in `seen`.
    Normally a file is 'ready' only once its .json sidecar exists (so we don't
    grab a half-written download); on the `final` sweep we take remaining media
    regardless, reading an empty sidecar if none was written."""
    for mpath in _pair_media(dest):
        if mpath in seen:
            continue
        has_side = os.path.exists(mpath + ".json")
        if has_side or final:
            seen.add(mpath)
            yield mpath, _read_sidecar(mpath)


def _pair_media(root):
    """All downloaded media files under root (recursive), excluding sidecars."""
    found = []
    for dirpath, _, names in os.walk(root):
        for n in names:
            ext = os.path.splitext(n)[1].lower()
            if ext in _MEDIA_EXTS:
                found.append(os.path.join(dirpath, n))
    return sorted(found)


def _read_sidecar(media_path):
    """gallery-dl writes `<media>.json` next to each file. Return it flattened,
    or {} if absent/unreadable — a missing sidecar shouldn't drop the image."""
    side = media_path + ".json"
    try:
        with open(side) as f:
            return _flatten(json.load(f))
    except (OSError, json.JSONDecodeError):
        return {}


def apply_mapping(meta, mapping):
    """Turn a gallery-dl metadata dict into the library's ingest packet using a
    saved per-site mapping of {source_field: target}.

    `target` is one of:
      "tags"        - source becomes tags. A list is used as-is; a scalar is
                      split on commas/whitespace. Multiple sources mapped to
                      "tags" are concatenated (deduped, order preserved).
      "tags:<pfx>"  - same as "tags", but every tag from this source is prefixed
                      with <pfx> for provenance, e.g. "tags:character:" turns
                      ["reimu","marisa"] into ["character:reimu","character:marisa"],
                      and "tags:artist_" gives ["artist_bob"]. The prefix is
                      literal text (whatever follows the first colon), so you
                      choose the separator. Dedup happens after prefixing, so the
                      same tag under two different prefixes is kept.
      "description" - source is appended to the description (newline-joined when
                      several sources map here). Non-str is JSON-stringified.
      "regions"     - source is a list of booru "note" dicts
                      ({x,y,width,height,body}); each becomes a normalized region
                      box (cx/cy/w/h in 0..1 + the note text as its description).
                      Needs the post's own width/height to normalize; those are
                      read from meta ("width"/"height", or "image_width"/... ).
      "exif:<Tag>"  - source is written to the named EXIF tag (e.g.
                      "exif:Artist", "exif:ImageDescription"). Collected into an
                      "exif" patch dict {TagName: value} that the ingest side
                      hands to exif_export.write_exif(), which validates the tag,
                      coerces the type, and silently skips unknown/read-only
                      tags. This opens up ~all of the EXIF schema as targets.
      "xmp:<Token>" - source is written to the named XMP property (e.g.
                      "xmp:Xmp.dc.creator", "xmp:dc.rights"). Collected into an
                      "xmp" patch dict {token: value} handed to
                      xmp_export.write_xmp(), which validates against the schema,
                      coerces by type, and MERGES into the sidecar the core
                      write already produced. Opens up ~all of the XMP schema
                      (dc, iptcCore, iptcExt, cc, prism, …) as targets.
                      (iptc: is reserved for when an IPTC writer exists.)
      anything else - passthrough: stored under that key in the packet verbatim,
                      so extra booru fields can ride along untouched.

    Every discovered field can therefore be routed somewhere (or left unmapped,
    which just skips it). Unknown/missing source fields are skipped, never fatal.
    """
    out = {"tags": [], "description": "", "regions": [], "exif": {}, "xmp": {}}
    descs = []
    for src, target in (mapping or {}).items():
        if not src or not target or target == "ignore" or src not in meta:
            continue
        val = meta[src]
        if target == "tags" or target.startswith("tags:"):
            # "tags" → no prefix; "tags:<pfx>" → literal prefix on each tag.
            prefix = target[len("tags:"):] if target.startswith("tags:") else ""
            out["tags"] += [prefix + t for t in _as_tags(val)]
        elif target == "description":
            descs.append(val if isinstance(val, str) else json.dumps(val,
                         ensure_ascii=False))
        elif target == "regions":
            out["regions"] += _notes_to_regions(val, meta)
        elif target.startswith("exif:"):
            tag = target[len("exif:"):]
            if tag:
                # write_exif wants scalars; join lists (booru tag lists) into a
                # space-separated string, leave scalars as-is for it to coerce.
                out["exif"][tag] = (" ".join(str(v) for v in val)
                                    if isinstance(val, list) else val)
        elif target.startswith("xmp:"):
            tok = target[len("xmp:"):]
            if tok:
                # write_xmp coerces per the property's type (list vs scalar vs
                # lang-alt), so pass the raw value through unchanged.
                out["xmp"][tok] = val
        else:
            out[target] = val if isinstance(val, str) else json.dumps(val,
                          ensure_ascii=False)

    # dedupe tags, preserve first-seen order
    seen, deduped = set(), []
    for t in out["tags"]:
        if t not in seen:
            seen.add(t); deduped.append(t)
    out["tags"] = deduped
    if descs:
        out["description"] = "\n".join(d for d in descs if d)
    # drop empty structural keys so they don't override existing file metadata
    if not out["regions"]:
        out.pop("regions")
    if not out["exif"]:
        out.pop("exif")
    if not out["xmp"]:
        out.pop("xmp")
    return out


def _as_tags(val):
    """A source value → list of tag strings."""
    if isinstance(val, list):
        return [str(t).strip() for t in val if str(t).strip()]
    return [t for t in str(val).replace(",", " ").split() if t]


def _notes_to_regions(notes, meta):
    """Convert a list of booru note/translation dicts into region boxes.

    Booru notes give pixel coords {x, y, width, height, body} against the full
    image; the app's regions are normalized center-form (cx, cy, w, h in 0..1)
    with a text description. We normalize using the post's own pixel dimensions
    (several key spellings seen across sites). Notes without usable geometry are
    skipped rather than emitted at bad coordinates.
    """
    if not isinstance(notes, list):
        return []
    iw = _first_num(meta, ("width", "image_width", "file.width", "file_width"))
    ih = _first_num(meta, ("height", "image_height", "file.height", "file_height"))
    if not iw or not ih:
        return []  # can't place boxes without image dimensions
    regions = []
    for n in notes:
        if not isinstance(n, dict):
            continue
        x = _num(n.get("x")); y = _num(n.get("y"))
        w = _num(n.get("width")); h = _num(n.get("height"))
        if None in (x, y, w, h) or w <= 0 or h <= 0:
            continue
        body = n.get("body") or n.get("text") or ""
        regions.append({
            "cx": max(0.0, min(1.0, (x + w / 2) / iw)),
            "cy": max(0.0, min(1.0, (y + h / 2) / ih)),
            "w":  max(0.0, min(1.0, w / iw)),
            "h":  max(0.0, min(1.0, h / ih)),
            "class_name": "translation",
            "region_name": (str(body)[:60] or "note"),
            "region_desc": str(body),
            "confirmed": True,
        })
    return regions


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _first_num(meta, keys):
    for k in keys:
        if k in meta:
            n = _num(meta[k])
            if n:
                return n
    return None


if __name__ == "__main__":
    # Self-check: pure-logic parts run offline (no network, no live extractor).
    assert _flatten({"a": 1, "b": {"c": 2, "d": {"e": 3}}}) == \
        {"a": 1, "b.c": 2, "b.d.e": 3}, "flatten nested failed"

    m = {"tag_string": "1girl solo blue_sky", "rating": "s",
         "note": {"body": "hi"}}
    fm = _flatten(m)
    assert fm["note.body"] == "hi"
    mapped = apply_mapping(fm, {"tag_string": "tags", "rating": "description"})
    assert mapped["tags"] == ["1girl", "solo", "blue_sky"], mapped
    assert mapped["description"] == "s", mapped

    assert apply_mapping({"tags": ["a", "b"]}, {"tags": "tags"})["tags"] == ["a", "b"]
    assert apply_mapping({}, {"nope": "tags"})["tags"] == []
    assert apply_mapping({"x": "1"}, {"x": "ignore"}) == {"tags": [], "description": ""}

    # multiple sources → tags: concatenated + deduped, order preserved
    multi = apply_mapping(
        {"a": "1girl solo", "b": ["solo", "sky"]},
        {"a": "tags", "b": "tags"})
    assert multi["tags"] == ["1girl", "solo", "sky"], multi

    # tags:<prefix> prefixes each tag; same tag under different prefixes kept
    pfx = apply_mapping(
        {"chars": ["reimu", "marisa"], "artist": "zun", "general": ["solo"]},
        {"chars": "tags:character:", "artist": "tags:artist_", "general": "tags"})
    assert "character:reimu" in pfx["tags"] and "character:marisa" in pfx["tags"], pfx
    assert "artist_zun" in pfx["tags"] and "solo" in pfx["tags"], pfx
    # a bare-tag and same value prefixed coexist (dedup is post-prefix)
    coex = apply_mapping({"a": ["x"], "b": ["x"]},
                         {"a": "tags", "b": "tags:src:"})
    assert coex["tags"] == ["x", "src:x"], coex

    # multiple sources → description: newline-joined
    dd = apply_mapping({"c": "char note", "d": "artist note"},
                       {"c": "description", "d": "description"})
    assert dd["description"] == "char note\nartist note", dd

    # passthrough target keeps arbitrary fields
    pt = apply_mapping({"src": "http://x/y"}, {"src": "source_url"})
    assert pt["source_url"] == "http://x/y", pt

    # exif: target collects into an exif patch; list source is space-joined
    ex = apply_mapping({"author": "bob", "chars": ["a", "b"]},
                       {"author": "exif:Artist", "chars": "exif:XPKeywords"})
    assert ex["exif"] == {"Artist": "bob", "XPKeywords": "a b"}, ex
    # no exif mappings -> no exif key
    assert "exif" not in apply_mapping({"a": "1"}, {"a": "tags"})

    # xmp: target collects into an xmp patch, raw value preserved for write_xmp
    xm = apply_mapping({"artist": "bob", "chars": ["a", "b"]},
                       {"artist": "xmp:Xmp.dc.creator", "chars": "xmp:dc.subject"})
    assert xm["xmp"] == {"Xmp.dc.creator": "bob", "dc.subject": ["a", "b"]}, xm
    assert "xmp" not in apply_mapping({"a": "1"}, {"a": "tags"})

    # notes → regions: e621-style {x,y,width,height,body} normalized to cx/cy/w/h
    notes_meta = {
        "width": 100, "height": 200,
        "notes": [{"x": 10, "y": 20, "width": 30, "height": 40, "body": "hi"},
                  {"x": 0, "y": 0, "width": 0, "height": 5}],  # bad geom -> skip
    }
    rr = apply_mapping(notes_meta, {"notes": "regions"})
    assert len(rr["regions"]) == 1, rr
    r = rr["regions"][0]
    assert abs(r["cx"] - 0.25) < 1e-9 and abs(r["cy"] - 0.20) < 1e-9, r
    assert abs(r["w"] - 0.30) < 1e-9 and abs(r["h"] - 0.20) < 1e-9, r
    assert r["region_desc"] == "hi" and r["class_name"] == "translation", r
    # no dimensions -> no regions, and empty regions key is dropped
    assert "regions" not in apply_mapping({"notes": [{"x":1,"y":1,"width":1,"height":1}]},
                                          {"notes": "regions"})

    kv = _opts_to_kvlist(["extractor.danbooru.username=me", "bad", "n=5",
                          "flag=true"])
    assert (("extractor", "danbooru"), "username", "me") in kv, kv
    assert (("extractor",), "n", 5) in kv, kv          # JSON-coerced int
    assert (("extractor",), "flag", True) in kv, kv    # JSON-coerced bool
    assert all("bad" not in str(t) for t in kv), kv    # no '=' -> dropped
    print("gdl self-check OK")