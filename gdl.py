"""gallery-dl integration — thin wrapper around the gallery-dl CLI.

Two jobs, nothing more:

  discover_fields(url)  -> {"site": str, "fields": [str, ...]}
        Runs `gallery-dl -j URL` (no download) and returns the flat, sorted
        union of metadata keys gallery-dl exposes for that URL, plus the
        extractor category so a mapping can be keyed per-site. This is what the
        settings UI shows the first time you add a site: pick which gdl field
        feeds `tags` vs `description`.

  download(url, dest)   -> yields (image_path, metadata_dict)
        Runs gallery-dl into `dest` with per-file JSON sidecars, then yields
        each downloaded media file paired with its parsed metadata. The caller
        maps the metadata onto the library's own tags/description via a saved
        site mapping and hands the file to the normal ingest path.

We shell out on purpose. gallery-dl is a mature, self-updating tool with
hundreds of site extractors; re-implementing any of that would be the opposite
of lazy. If the binary isn't installed, callers get a clear error.

A note on what `--write-metadata` actually writes: with no metadata options
configured it dumps the full *public* metadata dict (every key except the
`_`-prefixed private ones) — that's what we want, since the whole point is to
let the user map ANY exposed field. But that "no options configured" only holds
if the user's own gallery-dl config file isn't narrowing it: a
`metadata.include`/`fields`/`exclude` in their `~/.config/gallery-dl/config.json`
would silently trim what we see, breaking discovery and quietly dropping fields
at fetch time. So every invocation here passes `--config-ignore`: we don't
inherit their ambient config, and the sidecar is always the complete field set.
Credentials that DO live in user config (booru API keys, cookies) are passed
explicitly by the caller when needed rather than inherited implicitly.
"""
import json
import os
import shutil
import subprocess
import tempfile

# Media extensions gallery-dl may drop next to its .json sidecars. Anything not
# in here (its own .json, .txt notes) is ignored when pairing files to metadata.
_MEDIA_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".webm", ".mp4", ".mkv", ".avif",
    ".jxl", ".bmp", ".tiff", ".tif", ".mov", ".m4v", ".apng", ".svg",
}


class GdlError(RuntimeError):
    pass


def available():
    """True if the gallery-dl CLI is on PATH."""
    return shutil.which("gallery-dl") is not None


def _require():
    if not available():
        raise GdlError(
            "gallery-dl is not installed. Install it with "
            "`pip install gallery-dl` (it's in requirements.txt).")


def _run(args, timeout):
    """Run gallery-dl with args, return CompletedProcess. Raises on non-zero
    only when there is no usable stdout — some extractors warn to stderr but
    still emit valid JSON, and we'd rather use the data than fail the request.

    `--config-ignore` is prepended to every call so the user's own
    gallery-dl config can't narrow the metadata we get (see module docstring).
    Site credentials, when needed, are passed by the caller as explicit `-o`
    options rather than inherited from that config.
    """
    _require()
    try:
        return subprocess.run(
            ["gallery-dl", "--config-ignore", *args],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise GdlError(f"gallery-dl timed out after {timeout}s.")
    except FileNotFoundError:
        raise GdlError("gallery-dl binary vanished from PATH.")


def _flatten(obj, prefix=""):
    """Flatten one level of nested dicts into dotted keys, so a booru's
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


def discover_fields(url, timeout=60, opts=None):
    """Return the metadata fields available for `url` as
    {"site": category, "fields": sorted([...])}.

    First tries `gallery-dl -j` (no download, fast). For many URLs — especially
    a booru search or a user page — `-j` only resolves the *parent* queue entry
    and exposes a couple of fields, not the per-post metadata that actually
    matters. When that happens we fall back to downloading a single item with
    its sidecar (the same real metadata the fetch will use) and read the fields
    from there. That download IS the ground truth, so discovery never lies about
    what a post exposes.

    `opts` is an optional list of gallery-dl `-o KEY=VALUE` option strings —
    used to pass site credentials explicitly (e.g. an API key/username) since
    --config-ignore means we don't inherit them from the user's config file.
    """
    proc = _run(["-j", "--no-download", *_opts(opts), url], timeout)
    fields, site = _keys_from_dump(proc.stdout)

    # Too shallow to be a real post schema → resolve one item for its sidecar.
    if len(fields) < 4:
        s2, f2 = _keys_from_sample(url, timeout, opts)
        if len(f2) > len(fields):
            fields, site = f2, (s2 or site)

    if not fields:
        raise GdlError(proc.stderr.strip() or
                       "gallery-dl found no metadata fields for that URL.")
    return {"site": site, "fields": sorted(fields)}


def _opts(opts):
    """Expand a list of "KEY=VALUE" strings into gallery-dl -o arguments."""
    out = []
    for o in opts or []:
        if o:
            out += ["-o", o]
    return out


def _keys_from_dump(stdout):
    """Union of flattened keys + first category from a `-j` dump. ([], '') if
    the dump is empty or unparseable — the caller decides whether to fall back."""
    if not stdout.strip():
        return set(), ""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return set(), ""
    fields, site = set(), ""
    for msg in data if isinstance(data, list) else [data]:
        for part in (msg if isinstance(msg, list) else [msg]):
            if isinstance(part, dict):
                flat = _flatten(part)
                fields.update(flat.keys())
                site = site or flat.get("category") or flat.get("subcategory") or ""
    return fields, site


def _keys_from_sample(url, timeout, opts=None):
    """Download just the first item with its sidecar into a temp dir and read
    the real per-post fields from it. Returns (site, set_of_fields)."""
    tmp = tempfile.mkdtemp(prefix="gdl-probe-")
    try:
        _run(["--range", "1", "--write-metadata", "-D", tmp, *_opts(opts), url],
             timeout)
        for media in _pair_media(tmp):
            meta = _read_sidecar(media)
            if meta:
                return meta.get("category", ""), set(meta.keys())
        return "", set()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def site_of(url, timeout=60, opts=None):
    """The extractor category for a URL (e.g. 'danbooru'). Cheap: only the `-j`
    dump, never the sample download — good enough to pick per-site opts, and a
    miss just means we fall back to global-only opts."""
    proc = _run(["-j", "--no-download", *_opts(opts), url], timeout)
    _, site = _keys_from_dump(proc.stdout)
    return site


def download(url, dest, timeout=1800, opts=None):
    """Download everything at `url` into `dest` with JSON sidecars, then yield
    (media_path, metadata_dict) for each downloaded file. `dest` should be an
    empty/temp dir owned by the caller. `opts` is an optional list of
    "KEY=VALUE" gallery-dl option strings (e.g. credentials)."""
    os.makedirs(dest, exist_ok=True)
    proc = _run(
        ["--write-metadata", "-D", dest, *_opts(opts), url], timeout)
    # A non-zero exit with nothing downloaded is a real failure; a non-zero exit
    # with files on disk usually means "some posts skipped" — proceed with what
    # we got and surface the tail of stderr only if literally nothing landed.
    media = _pair_media(dest)
    if not media:
        raise GdlError(proc.stderr.strip()[-500:] or
                       "gallery-dl downloaded nothing.")
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
    """gallery-dl writes `<media>.json` next to each file. Return it parsed, or
    {} if absent/unreadable — a missing sidecar shouldn't drop the image."""
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
    or missing source fields are simply skipped.
    """
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
    # Self-check: the pure-logic parts (flatten + mapping) run offline. Network
    # bits (discover/download) need a live URL and the binary, so they're not
    # asserted here — this guards the parsing that actually has branches.
    assert _flatten({"a": 1, "b": {"c": 2, "d": {"e": 3}}}) == \
        {"a": 1, "b.c": 2, "b.d.e": 3}, "flatten nested failed"

    m = {"tag_string": "1girl solo blue_sky", "rating": "s",
         "note": {"body": "hi"}}
    fm = _flatten(m)
    assert fm["note.body"] == "hi"
    mapped = apply_mapping(
        fm, {"tags": "tag_string", "description": "rating"})
    assert mapped["tags"] == ["1girl", "solo", "blue_sky"], mapped
    assert mapped["description"] == "s", mapped

    mapped2 = apply_mapping({"tags": ["a", "b"]}, {"tags": "tags"})
    assert mapped2["tags"] == ["a", "b"], mapped2

    # missing source field is skipped, not crashed
    assert apply_mapping({}, {"tags": "nope"})["tags"] == []
    assert _opts(["a=1", "", "b=2"]) == ["-o", "a=1", "-o", "b=2"], _opts(["a=1","","b=2"])
    assert _opts(None) == []
    print("gdl self-check OK")