"""! @file
@brief Front-end bundles: the page's scripts and the enabled modules' assets
concatenated (minified when rjsmin / rcssmin are installed) into one file each,
named by content hash so the browser caches them for a year and precompressed
with gzip. One page load is then 3 asset requests instead of ~130 revalidations.

The bundles are rebuilt at every start (milliseconds), so an edited file is
always picked up; `manager.py --no-bundle` serves the individual files instead
(handy with the browser's debugger).
"""
import glob
import gzip
import hashlib
import logging
import os
import time

try:
    import rjsmin
except ImportError:
    rjsmin = None
try:
    import rcssmin
except ImportError:
    rcssmin = None

ROOT = os.path.dirname(os.path.abspath(__file__))
BUILD_DIR = os.path.join(ROOT, "static", "build")
URL_PREFIX = "/static/build/"

## @brief The page's own scripts, in load order (app.html emits them from here).
CORE_JS = [
    "auth.js", "features.js", "globals.js", "scroll_keep.js", "viewer.js", "gallery.js",
    "upload.js", "albums.js", "panes.js", "panes_resize.js",
    "vendor/three.min.js", "vendor/OBJLoader.js", "vendor/OrbitControls.js",
    "editor.js", "ai_tools.js", "review.js", "tiers.js", "info.js", "user_settings.js",
    "modules.js", "init.js", "controls_tabs.js",
]


def _minify(kind, text):
    try:
        if kind == "js" and rjsmin is not None:
            return rjsmin.jsmin(text, keep_bang_comments=True)
        if kind == "css" and rcssmin is not None:
            return rcssmin.cssmin(text, keep_bang_comments=True)
    except Exception:
        pass
    return text


def _bundle(name, kind, files, log):
    """! @brief Write static/build/<name>.<hash>.<kind> (+ .gz) from `files`
    [(label, abs_path)]. @return the bundle's URL, or None when nothing was bundled."""
    parts, missing = [], []
    for label, path in files:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            missing.append(label)
            continue
        text = _minify(kind, text)
        # a script that ends without ';' must not swallow the next one's first statement
        parts.append(f"/* {label} */\n{text}\n" + (";\n" if kind == "js" else "\n"))
    if not parts:
        return None
    body = "".join(parts).encode("utf-8")
    digest = hashlib.sha1(body).hexdigest()[:12]
    fn = f"{name}.{digest}.{kind}"
    out = os.path.join(BUILD_DIR, fn)
    if not os.path.exists(out):
        tmp = out + ".tmp"
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, out)
        with open(tmp, "wb") as f:
            f.write(gzip.compress(body, 9, mtime=0))
        os.replace(tmp, out + ".gz")
    if missing:
        log.warning(f"bundle {name}: missing {', '.join(missing)}")
    return URL_PREFIX + fn


def build(module_assets, log=None):
    """! @brief Build the three bundles.
    @param module_assets  [{url, kind, module_id}] of the enabled modules, in load
                          order (what /api/module_assets serves), url being
                          /modules/<id>/static/<file> or /static/<file>.
    @return {"core_js": url, "modules_js": url | None, "modules_css": url | None}.
    """
    log = log or logging.getLogger("access")
    t0 = time.time()
    os.makedirs(BUILD_DIR, exist_ok=True)
    keep = set()
    out = {}
    out["core_js"] = _bundle("core", "js", [(f, os.path.join(ROOT, "static", f)) for f in CORE_JS], log)
    js, css = [], []
    for a in module_assets:
        url = a["url"]
        if url.startswith("/modules/") and "/static/" in url:
            mid, _, fn = url[len("/modules/"):].partition("/static/")
            path = a.get("path") or os.path.join(ROOT, "modules", mid, "static", fn)
        elif url.startswith("/static/"):
            path = os.path.join(ROOT, "static", url[len("/static/"):])
        else:
            continue
        (css if a["kind"] == "css" else js).append((url, path))
    out["modules_js"] = _bundle("modules", "js", js, log)
    out["modules_css"] = _bundle("modules", "css", css, log)
    for u in out.values():
        if u:
            keep.add(os.path.basename(u))
    # drop bundles from earlier starts
    for old in glob.glob(os.path.join(BUILD_DIR, "*")):
        base = os.path.basename(old)
        if base.endswith(".gz"):
            base = base[:-3]
        if base not in keep:
            try:
                os.remove(old)
            except OSError:
                pass
    size = sum(os.path.getsize(os.path.join(BUILD_DIR, b)) for b in keep)
    log.info(f"bundles: {len(keep)} file(s), {size // 1024} KB"
             f"{'' if rjsmin else ' (rjsmin not installed: unminified)'} in {time.time() - t0:.2f}s")
    return out