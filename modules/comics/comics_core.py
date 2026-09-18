"""
Comics — a comic is a folder of ordered page images plus comic-level
metadata (comic.json is the portable source of truth; the `comics` table
and files.comic_folder are caches rebuilt from it on index). Moved out of
manager.py; core names are bound in register() (see _bind).

Also owns the archive side (cbz/cbr/cb7 read through the books module's
format hook, see comic_pages.py) and the comic reader modal + JS.
"""
import json
import os
import time

from flask import request, jsonify

COMIC_SCHEMA = "mm.comic/1"
_ROUTES = []


def _route(rule, **opts):
    def deco(fn):
        _ROUTES.append((rule, fn, opts))
        return fn
    return deco


def _feature(*a, **k):
    def deco(fn):
        fn._feature = (a, k)
        return fn
    return deco


HOST = None
_db = state = MEDIA_DIR = get_safe_path = read_jxl = _to_bgr = read_metadata = None
write_metadata = access_logger = _rel = mt = _llm_call = _run_pipeline_on = None
_apply_pipeline_result = DEFAULT_PIPELINE = None


def _bind(host):
    c = host.core
    globals().update({
        "HOST": host, "_db": host.db, "state": host.config, "MEDIA_DIR": host.media_dir,
        "get_safe_path": host.safe_path, "read_jxl": c.read_image, "_to_bgr": c.to_bgr,
        "read_metadata": c.read_metadata, "write_metadata": c.write_metadata,
        "access_logger": host.logger, "_rel": c.rel, "mt": host.media, "_llm_call": c.llm_call,
        "_run_pipeline_on": c.run_pipeline, "_apply_pipeline_result": c.apply_pipeline_result,
        "DEFAULT_PIPELINE": c.default_pipeline,
    })


# in <folder>/comic.json (the portable source of truth); the `comics` table and
# the files.comic_folder column are caches rebuilt from it on index.

def _comic_json_path(folder: str) -> str:
    """! @brief Resolve a folder's comic.json path (safe-joined under MEDIA_DIR)."""
    rel = (folder + "/comic.json") if folder else "comic.json"
    return get_safe_path(MEDIA_DIR, rel)

def _auto_pages(folder: str) -> list:
    """!
    @brief List library asset filenames directly inside a folder, sorted.
    @return Relative filenames (images/video); [] if the folder is missing.
    """
    base = get_safe_path(MEDIA_DIR, folder) if folder else os.path.abspath(MEDIA_DIR)
    if not base or not os.path.isdir(base):
        return []
    return sorted(f for f in os.listdir(base)
                  if mt.is_library_file(f) and os.path.isfile(os.path.join(base, f)))

def _load_comic_json(folder: str) -> dict | None:
    """! @brief Load a folder's comic.json, or None if absent/unreadable."""
    p = _comic_json_path(folder)
    if not p or not os.path.exists(p):
        return None
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        access_logger.warning(f"_load_comic_json {folder}: {e}")
        return None

def _write_comic_json(folder: str, data: dict) -> bool:
    """! @brief Write a folder's comic.json. @return True on success."""
    p = _comic_json_path(folder)
    if not p:
        return False
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return True

def _set_comic_membership(folder: str) -> None:
    """! @brief Flag a folder's page files as comic members so they leave the flat gallery."""
    if not folder:
        return
    _db().execute(
        "UPDATE files SET comic_folder=? WHERE rel_path LIKE ? AND rel_path NOT LIKE ?",
        (folder, folder + '/%', folder + '/%/%'))
    _db().commit()

def _write_comic_page_count(folder: str, data: dict) -> None:
    """!
    @brief Write prism:PageCount into the comic's cover-page XMP so the count travels with the file.
    @note Best-effort; leaves tags/description/regions untouched. No-op if the cover can't be resolved.
    """
    try:
        pages = _comic_ordered_pages(folder, data)
        if not pages:
            return
        cover = data.get("cover") or pages[0]
        cover_rel = f"{folder}/{cover}" if folder else cover
        fp = get_safe_path(MEDIA_DIR, cover_rel)
        if not fp or not os.path.exists(fp):
            return
        meta = read_metadata(fp)
        write_metadata(fp, meta.get("tags", []), meta.get("description", ""),
                       meta.get("regions", []), page_count=len(pages))
    except Exception as e:
        access_logger.warning(f"_write_comic_page_count {folder}: {e}")

def _upsert_comic_row(folder, data):
    pages = data.get("pages") or _auto_pages(folder)
    _db().execute("""
        INSERT INTO comics(folder,title,author,description,tags,characters,cover,page_order,created,mtime)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(folder) DO UPDATE SET
            title=excluded.title, author=excluded.author, description=excluded.description,
            tags=excluded.tags, characters=excluded.characters, cover=excluded.cover,
            page_order=excluded.page_order, mtime=excluded.mtime
    """, (folder, data.get("title", ""), data.get("author", ""), data.get("description", ""),
          json.dumps(data.get("tags", [])), json.dumps(data.get("characters", [])),
          data.get("cover", pages[0] if pages else ""), json.dumps(pages),
          data.get("created", time.time()), time.time()))
    _db().commit()

def _comic_ordered_pages(folder, data=None):
    """Declared order, dropping missing files and appending any new ones."""
    data = data or _load_comic_json(folder) or {}
    declared = data.get("pages") or []
    auto = _auto_pages(folder)
    ordered = [p for p in declared if p in auto] + [p for p in auto if p not in declared]
    return ordered

def _scan_comics() -> None:
    """! @brief Walk MEDIA_DIR for comic.json files and rebuild the comics cache."""
    found = {}
    for root, dirs, files in os.walk(MEDIA_DIR):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != 'runs']
        if 'comic.json' in files:
            rel = _rel(root)
            if rel == '.':
                continue   # don't treat the whole library as one comic
            data = _load_comic_json(rel)
            if data is not None:
                found[rel] = data
    existing = {r["folder"] for r in _db().execute("SELECT folder FROM comics").fetchall()}
    for folder, data in found.items():
        _upsert_comic_row(folder, data)
        _set_comic_membership(folder)
    for gone in existing - set(found):
        _db().execute("DELETE FROM comics WHERE folder=?", (gone,))
        _db().execute("UPDATE files SET comic_folder='' WHERE comic_folder=?", (gone,))
    _db().commit()
    access_logger.info(f"Comic scan: {len(found)} comic(s)")

def _comic_folder_set() -> set:
    """! @brief Set of all folders currently registered as comics."""
    return {r["folder"] for r in _db().execute("SELECT folder FROM comics").fetchall()}


def _merge_comic_analyses(folder, page_analyses, summarize=True):
    """Aggregate per-page pipeline analyses into comic-level metadata.

    - tags: union across all pages (order-preserving, deduped)
    - characters: distinct subject labels across pages, each with the longest
      per-page description seen for that label (a reasonable 'best' blurb)
    - description: per-page scene summaries joined into a synopsis; if
      `summarize` and an LLM is configured, condensed into a short series blurb
    Writes the result into comic.json + the comics DB row. Returns the dict.
    """
    all_tags, seen = [], set()
    characters = {}            # label -> best (longest) description
    page_lines = []
    for idx, (page, analysis) in enumerate(page_analyses):
        for t in analysis.get("tags", []):
            if t and t.lower() not in seen:
                all_tags.append(t); seen.add(t.lower())
        for s in analysis.get("subjects", []):
            label = (s.get("label") or "").strip()
            if not label:
                continue
            desc = (s.get("detail") or "").strip()
            if label not in characters or len(desc) > len(characters[label]):
                characters[label] = desc
        scene = (analysis.get("summary") or "").strip()
        if scene:
            page_lines.append(f"Page {idx + 1}: {scene}")

    synopsis = "\n".join(page_lines)
    if summarize and synopsis and state.get("oai_endpoint") and state.get("oai_model"):
        try:
            prompt = ("Below are one-line summaries of each page of a comic, in order. "
                      "Write a short synopsis (2-4 sentences) of the comic as a whole.\n\n"
                      + synopsis)
            condensed = (_llm_call(prompt, None, "text") or "").strip()
            if condensed:
                synopsis = condensed
        except Exception as e:
            access_logger.warning(f"comic synopsis {folder}: {e}")

    data = _load_comic_json(folder) or {}
    data["tags"] = all_tags
    data["characters"] = sorted(characters.keys())
    data["character_notes"] = characters          # label -> blurb
    data["description"] = synopsis
    if "pages" not in data:
        data["pages"] = _comic_ordered_pages(folder)
    data.setdefault("schema", COMIC_SCHEMA)
    _write_comic_json(folder, data)
    _upsert_comic_row(folder, data)
    return data


@_route("/api/comic")
def api_comic_get():
    folder = request.args.get("folder", "").strip()
    data = _load_comic_json(folder)
    if data is None:
        return jsonify({"success": False, "error": "Not a comic."})
    pages = _comic_ordered_pages(folder, data)
    return jsonify({"success": True,
                    "comic": {"folder": folder,
                              "title": data.get("title", ""),
                              "author": data.get("author", ""),
                              "description": data.get("description", ""),
                              "tags": data.get("tags", []),
                              "characters": data.get("characters", []),
                              "cover": data.get("cover", pages[0] if pages else "")},
                    "pages": [folder + "/" + p for p in pages]})

@_route("/api/comic_create", methods=["POST"])
@_feature("comics.make", level="write", action='comic_create', fields=('folder', 'title'))
def api_comic_create():
    d = request.json or {}
    folder = (d.get("folder", "") or "").strip().strip('/')
    if not folder:
        return jsonify({"success": False, "error": "A folder is required."})
    if not get_safe_path(MEDIA_DIR, folder):
        return jsonify({"success": False, "error": "Invalid folder."})
    pages = _auto_pages(folder)
    if not pages:
        return jsonify({"success": False, "error": "Folder has no images."})
    data = {"schema": COMIC_SCHEMA,
            "title": d.get("title") or folder.split('/')[-1],
            "author": d.get("author", ""), "description": d.get("description", ""),
            "tags": d.get("tags", []), "characters": d.get("characters", []),
            "cover": pages[0], "pages": pages, "created": time.time()}
    if not _write_comic_json(folder, data):
        return jsonify({"success": False, "error": "Could not write comic.json."})
    _upsert_comic_row(folder, data)
    _set_comic_membership(folder)
    _write_comic_page_count(folder, data)
    return jsonify({"success": True, "folder": folder})

@_route("/api/comic_update", methods=["POST"])
@_feature("comics.edit", level="write", action='comic_update', fields=('folder', 'title'))
def api_comic_update():
    d = request.json or {}
    folder = (d.get("folder", "") or "").strip().strip('/')
    data = _load_comic_json(folder)
    if data is None:
        return jsonify({"success": False, "error": "Not a comic."})
    for k in ("title", "author", "description", "tags", "characters", "cover", "pages"):
        if k in d:
            data[k] = d[k]
    data["schema"] = COMIC_SCHEMA
    _write_comic_json(folder, data)
    _upsert_comic_row(folder, data)
    _write_comic_page_count(folder, data)
    return jsonify({"success": True})

@_route("/api/comic_delete", methods=["POST"])
@_feature("comics.delete", level="write", action='comic_delete', fields=('folder',))
def api_comic_delete():
    """Unpackage a comic (keeps all images, just removes comic status)."""
    folder = (request.json.get("folder", "") or "").strip().strip('/')
    p = _comic_json_path(folder)
    if p and os.path.exists(p):
        os.remove(p)
    _db().execute("DELETE FROM comics WHERE folder=?", (folder,))
    _db().execute("UPDATE files SET comic_folder='' WHERE comic_folder=?", (folder,))
    _db().commit()
    return jsonify({"success": True})

@_route("/api/comic_pipeline", methods=["POST"])
@_feature("ai.smarttag", level="write")
def comic_pipeline_route():
    """Run the pipeline across every page of a comic IN ORDER, store each page's
    result, then merge tags / characters / description up to the comic level.
    Expects {"folder": "<comic folder rel path>"}. Uses the comic pipeline tree
    if configured (state['comic_pipeline_tree']), else the default tree."""
    folder = (request.json.get("folder") or "").strip().strip("/")
    if not folder:
        return jsonify({"success": False, "error": "No comic folder given."})
    if not state.get("oai_endpoint") or not state.get("oai_model"):
        return jsonify({"success": False, "error": "LLM not configured."})
    pages = _comic_ordered_pages(folder)
    if not pages:
        return jsonify({"success": False, "error": "No pages found in comic."})
    tree = state.get("comic_pipeline_tree") or state.get("pipeline_tree") or DEFAULT_PIPELINE
    total = len(pages)
    page_analyses, errors = [], []
    for i, page in enumerate(pages):
        rel = f"{folder}/{page}"
        fp = get_safe_path(MEDIA_DIR, rel)
        if not fp or not os.path.exists(fp):
            errors.append(page); continue
        try:
            img = read_jxl(fp)
            if img is None:
                errors.append(page); continue
            def _prog(msg, i=i): state["status_text"] = f"Comic {i+1}/{total}: {msg}"
            analysis = _run_pipeline_on(_to_bgr(img), fp, tree, _prog)
            _apply_pipeline_result(fp, analysis)      # store per-page result too
            page_analyses.append((page, analysis))
        except Exception as e:
            errors.append(page)
            access_logger.error(f"comic_pipeline {rel}: {e}")
    state["status_text"] = "Merging comic…"
    merged = _merge_comic_analyses(folder, page_analyses,
                                   summarize=request.json.get("summarize", True))
    state["status_text"] = "Ready."
    return jsonify({"success": True, "pages_done": len(page_analyses),
                    "errors": errors, "comic": {
                        "tags": merged.get("tags", []),
                        "characters": merged.get("characters", []),
                        "description": merged.get("description", "")}})
