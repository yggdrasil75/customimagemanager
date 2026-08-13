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
import os
import shutil
import tempfile
import threading

import gallery_dl.config as _gconfig
from gallery_dl import exception as _gexc
from gallery_dl.extractor import find as _find_extractor
from gallery_dl.job import DataJob, DownloadJob

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
    return _gconfig.apply(_opts_to_kvlist(opts))


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
        err = job.exception

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


def download(url, dest, opts=None):
    """Download everything at `url` into `dest` (flat, with JSON sidecars) via a
    DownloadJob, then yield (media_path, metadata_dict) for each file. `dest`
    should be an empty/temp dir owned by the caller."""
    os.makedirs(dest, exist_ok=True)
    if not _find_extractor(url):
        raise GdlError(f"No gallery-dl extractor matches that URL: {url}")

    # Force our output dir, a flat layout (no per-site subfolders), and a
    # full-metadata JSON sidecar per file. These sit under ("extractor",) so
    # they apply to whatever extractor the URL resolves to.
    pin = [
        (("extractor",), "base-directory", dest),
        (("extractor",), "directory", []),
        (("extractor",), "postprocessors",
         [{"name": "metadata", "mode": "json"}]),
    ]
    with _lock:
        _gconfig.clear()
        with _gconfig.apply(pin + _opts_to_kvlist(opts)):
            job = DownloadJob(url)
            job.run()
            err = job.exception

    media = _pair_media(dest)
    if not media:
        raise GdlError(str(err) if err else "gallery-dl downloaded nothing.")
    for mpath in media:
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
    """Turn a gallery-dl metadata dict into the library's own packet using a
    saved per-site mapping: {"tags": "<field>", "description": "<field>", ...}.

    A mapped tags field that is a list becomes the tag list; a scalar is split
    on commas/whitespace. description/other targets are stringified. Unmapped
    or missing source fields are simply skipped."""
    out = {"tags": [], "description": ""}
    for target, src in (mapping or {}).items():
        if not src or src not in meta:
            continue
        val = meta[src]
        if target == "tags":
            if isinstance(val, list):
                out["tags"] = [str(t).strip() for t in val if str(t).strip()]
            else:
                out["tags"] = [t for t in str(val).replace(",", " ").split() if t]
        else:
            out[target] = val if isinstance(val, str) else json.dumps(val)
    return out


if __name__ == "__main__":
    # Self-check: pure-logic parts run offline (no network, no live extractor).
    assert _flatten({"a": 1, "b": {"c": 2, "d": {"e": 3}}}) == \
        {"a": 1, "b.c": 2, "b.d.e": 3}, "flatten nested failed"

    m = {"tag_string": "1girl solo blue_sky", "rating": "s",
         "note": {"body": "hi"}}
    fm = _flatten(m)
    assert fm["note.body"] == "hi"
    mapped = apply_mapping(fm, {"tags": "tag_string", "description": "rating"})
    assert mapped["tags"] == ["1girl", "solo", "blue_sky"], mapped
    assert mapped["description"] == "s", mapped

    assert apply_mapping({"tags": ["a", "b"]}, {"tags": "tags"})["tags"] == ["a", "b"]
    assert apply_mapping({}, {"tags": "nope"})["tags"] == []

    kv = _opts_to_kvlist(["extractor.danbooru.username=me", "bad", "n=5",
                          "flag=true"])
    assert (("extractor", "danbooru"), "username", "me") in kv, kv
    assert (("extractor",), "n", 5) in kv, kv          # JSON-coerced int
    assert (("extractor",), "flag", True) in kv, kv    # JSON-coerced bool
    assert all("bad" not in str(t) for t in kv), kv    # no '=' -> dropped
    print("gdl self-check OK")