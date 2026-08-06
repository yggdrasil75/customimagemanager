"""
book_routes.py — HTTP surface for the books/comics side.
========================================================

manager.py is already 8.5k lines. Rather than append a twelfth feature block to
it, the book endpoints live here and get mounted with one call:

    import book_routes
    book_routes.register(app, {
        "db": _db, "media_dir": MEDIA_DIR, "safe_path": get_safe_path,
        "embed_text": _oai_embed_text, "embed_enabled": _oai_embed_enabled,
        "embed_tag": _oai_embed_tag, "logger": access_logger,
    })

Everything this module needs from manager.py arrives through that dict, so there
is no import cycle and no duplicated DB/config logic. The music block in
manager.py stays where it is; this is the pattern to move toward, not a
retroactive demand that music be moved.

ENDPOINTS
─────────
  GET  /api/books/status                  counts + worker progress
  POST /api/books/reindex                 walk MEDIA_DIR, classify, upsert
  POST /api/books/extract                 extract text for books missing it
  POST /api/books/embed                   embed passages for search
  GET  /api/books/list                    browse/filter/paginate
  GET  /api/books/authors|series          browse facets
  GET  /api/books/detail                  one book, full metadata
  POST /api/books/meta                    edit metadata
  GET  /api/books/cover/<path>            cover JPEG (cached on disk)
  GET  /api/books/section/<path>          one section of a flow book (HTML)
  GET  /api/books/page/<path>             one page of a paged book (JPEG)
  GET  /api/books/toc/<path>              section titles
  GET/POST /api/books/progress            reading position
  GET/POST/DELETE /api/books/bookmarks    bookmarks + notes
  POST /api/books/search                  passage-level semantic search
  GET  /api/books/triage                  the "is this a book?" queue
  POST /api/books/triage/decide           answer one triage item
"""
from __future__ import annotations

import os
import json
import time
import threading

from flask import request, jsonify, send_file, Response

import book_index as bi


# Filled in by register().
CTX: dict = {}

book_state = {
    "indexing": False, "indexed": 0, "total": 0,
    "extracting": False, "ext_done": 0, "ext_total": 0,
    "embedding": False, "emb_done": 0, "emb_total": 0,
    "comic": False, "comic_done": 0, "comic_total": 0,
    "comic_book": "", "comic_stage": "",
    "status": "idle", "last_error": "",
}

# Set to ask the running comic job to stop. A 400-page volume with per-panel
# OCR is a long job, and starting one you can't stop is a trap.
_comic_cancel = threading.Event()


def _db():
    return CTX["db"]()


def _media():
    return CTX["media_dir"]


def _abs(rel):
    return CTX["safe_path"](_media(), rel)


def _log():
    return CTX["logger"]


def _cache_dir():
    d = os.path.join(_media(), ".bookcache")
    os.makedirs(d, exist_ok=True)
    return d


def _user():
    """Current username, or '' when auth is off. Progress is per-user so a
    shared library doesn't have two people fighting over one bookmark."""
    fn = CTX.get("current_user")
    try:
        return (fn() if callable(fn) else "") or ""
    except Exception:
        return ""


# ══════════════════════════════════════════════════════════════════════════════
# Indexing
# ══════════════════════════════════════════════════════════════════════════════

def _upsert_book(rel_path, abs_path, verdict, force=False) -> bool:
    """Index one book if new or changed. Returns True if (re)indexed."""
    try:
        st = os.stat(abs_path)
    except OSError:
        return False
    mtime, size = st.st_mtime, st.st_size

    db = _db()
    if not force:
        row = db.execute("SELECT mtime FROM books WHERE rel_path=?",
                         (rel_path,)).fetchone()
        if row and abs(row["mtime"] - mtime) < 1e-6:
            return False

    fmt = verdict.fmt or "unknown"
    kind = verdict.kind or "book"
    meta = bi.read_metadata(abs_path, fmt)

    # A text-free PDF is a scan: treat it as a comic so the reader picks
    # fit-width spreads instead of a font-size slider.
    if fmt == "pdf" and kind == "book":
        try:
            if bi.pdf_is_probably_comic(abs_path):
                kind = "comic"
        except Exception:
            pass

    cover_name = ""
    if meta.get("cover_bytes"):
        jpg = bi.make_cover_jpeg(meta["cover_bytes"])
        if jpg:
            cover_name = bi.cover_cache_name(rel_path)
            try:
                with open(os.path.join(_cache_dir(), cover_name), "wb") as f:
                    f.write(jpg)
            except Exception:
                cover_name = ""

    page_count = meta.get("page_count")
    if page_count is None:
        try:
            page_count = bi.page_count_for(abs_path, fmt)
        except Exception:
            page_count = None

    title = meta["title"] or os.path.splitext(os.path.basename(rel_path))[0]

    db.execute("""
        INSERT INTO books(rel_path, mtime, size, fmt, kind, reader,
                          title, sort_title, authors, series, series_index,
                          publisher, published, language, isbn, identifiers,
                          description, subjects, tags, page_count, cover,
                          source, added, indexed)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'[]',?,?,?,?,?)
        ON CONFLICT(rel_path) DO UPDATE SET
            mtime=excluded.mtime, size=excluded.size, fmt=excluded.fmt,
            kind=excluded.kind, reader=excluded.reader,
            title=CASE WHEN books.title='' THEN excluded.title ELSE books.title END,
            sort_title=excluded.sort_title,
            authors=CASE WHEN books.authors='[]' THEN excluded.authors ELSE books.authors END,
            series=CASE WHEN books.series='' THEN excluded.series ELSE books.series END,
            series_index=COALESCE(books.series_index, excluded.series_index),
            publisher=excluded.publisher, published=excluded.published,
            language=excluded.language, isbn=excluded.isbn,
            identifiers=excluded.identifiers,
            description=CASE WHEN books.description='' THEN excluded.description ELSE books.description END,
            subjects=excluded.subjects, page_count=excluded.page_count,
            cover=CASE WHEN excluded.cover!='' THEN excluded.cover ELSE books.cover END,
            source=excluded.source, indexed=excluded.indexed,
            text_status='pending'
    """, (rel_path, mtime, size, fmt, kind, bi.reader_for(fmt),
          title, bi.sort_title(title), json.dumps(meta["authors"]),
          meta["series"], meta["series_index"], meta["publisher"],
          meta["published"], meta["language"], meta["isbn"],
          json.dumps(meta["identifiers"]), meta["description"],
          json.dumps(meta["subjects"]), page_count, cover_name,
          meta["source"], time.time(), time.time()))

    db.execute("DELETE FROM book_authors WHERE rel_path=?", (rel_path,))
    for a in meta["authors"]:
        db.execute("INSERT OR IGNORE INTO book_authors(rel_path,author) VALUES(?,?)",
                   (rel_path, a.strip()))
    db.commit()
    return True


def _record_triage(rel_path, abs_path, verdict):
    """Park an undecidable file in the triage queue with enough context for a
    human to answer in one glance."""
    db = _db()
    row = db.execute("SELECT decision FROM book_triage WHERE rel_path=?",
                     (rel_path,)).fetchone()
    if row and row["decision"]:
        return          # already answered; don't nag
    preview = ""
    try:
        head = bi._read_head(abs_path, 2048)
        preview = bi._strip_tags(head.decode("utf-8", "replace"))[:400]
    except Exception:
        pass
    try:
        size = os.path.getsize(abs_path)
    except OSError:
        size = 0
    db.execute("""
        INSERT INTO book_triage(rel_path, ext, sniffed, reason, size, preview, created)
        VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(rel_path) DO UPDATE SET
            sniffed=excluded.sniffed, reason=excluded.reason,
            size=excluded.size, preview=excluded.preview
    """, (rel_path, os.path.splitext(rel_path)[1].lower(), verdict.fmt or "",
          verdict.reason, size, preview, time.time()))
    db.commit()


def _index_background(force=False):
    if book_state["indexing"]:
        return
    book_state["indexing"] = True
    book_state["status"] = "scanning"
    try:
        media = _media()
        found, triage = [], []
        for rel, ap, v in bi.walk_candidates(media):
            if v.status == "book":
                found.append((rel, ap, v))
            elif v.status == "triage":
                triage.append((rel, ap, v))
            # 'sidecar' / 'part' / 'skip' are silent by design — the whole point
            # is that a library full of .txt sidecars produces zero noise.

        # A file the user already said "yes, it's a book" about is promoted out
        # of triage on every rescan, so one decision sticks forever.
        db = _db()
        decided = {r["rel_path"]: r["decision"] for r in db.execute(
            "SELECT rel_path, decision FROM book_triage WHERE decision IS NOT NULL")}
        for rel, ap, v in list(triage):
            d = decided.get(rel)
            if d == "book":
                v.status = "book"
                v.fmt = v.fmt or bi.sniff(ap) or "text"
                found.append((rel, ap, v))
                triage.remove((rel, ap, v))
            elif d == "not_book":
                triage.remove((rel, ap, v))

        book_state["total"] = len(found)
        book_state["indexed"] = 0

        have = {rel for rel, _, _ in found}
        for (rp,) in db.execute("SELECT rel_path FROM books").fetchall():
            if rp not in have:
                _purge_book(rp)

        for rel, ap, v in found:
            try:
                _upsert_book(rel, ap, v, force=force)
            except Exception as e:
                _log().error(f"book index {rel}: {e}")
            book_state["indexed"] += 1

        for rel, ap, v in triage:
            try:
                _record_triage(rel, ap, v)
            except Exception as e:
                _log().error(f"book triage {rel}: {e}")

        book_state["status"] = "idle"
    except Exception as e:
        book_state["status"] = "error"
        book_state["last_error"] = str(e)
        _log().error(f"book index: {e}")
    finally:
        book_state["indexing"] = False


def reconcile():
    """Drop book rows whose file has vanished, then run an incremental scan.

    Called from manager's startup index pass so books stay in step with disk
    without the user pressing anything. Cheap on a warm library: the walk skips
    unchanged mtimes, so the real cost is one os.walk.
    """
    db = _db()
    gone = []
    for (rp,) in db.execute("SELECT rel_path FROM books").fetchall():
        ap = _abs(rp)
        if not ap or not os.path.exists(ap):
            gone.append(rp)
    for rp in gone:
        _purge_book(rp)
    if gone:
        _log().info(f"book reconcile purged {len(gone)} deleted books")
    _index_background(force=False)
    return len(gone)


def index_one(rel_path: str) -> dict:
    """Index exactly ONE book. Called by manager.api_upload.

    The uploader must not trigger a whole-tree walk per file -- a 3000-book bulk
    upload would start 3000 scans. This classifies the single file (with real
    directory context, so an uploaded .html next to a mimetype is still
    correctly seen as a chapter) and upserts it synchronously, so the upload
    response means "it's in the library and openable".

    Returns the Verdict as a dict so the caller can report a rejection instead
    of claiming success on a file that was actually a sidecar.
    """
    ap = _abs(rel_path)
    if not ap or not os.path.exists(ap):
        return {"status": "skip", "reason": "file not found"}
    v = bi.classify(ap)
    if v.status == "book":
        _upsert_book(rel_path, ap, v, force=True)
        # Extract text now too. It is the difference between "uploaded" and
        # "readable", and doing it lazily means the first person to open the
        # book pays a multi-second stall with no explanation.
        try:
            if bi.reader_for(v.fmt or "") == "flow":
                _extract_one(rel_path)
        except Exception as e:
            _log().warning(f"book extract after upload {rel_path}: {e}")
    elif v.status == "triage":
        _record_triage(rel_path, ap, v)
    return v.as_dict()


def rename_book(old_rel: str, new_rel: str) -> bool:
    """Repoint every book table from old_rel to new_rel. Called by api_move.

    rel_path is the primary key across books, book_authors, book_sections,
    book_chunks, book_progress and book_bookmarks. Re-indexing at the new path
    instead of renaming would work for the metadata but would silently discard
    the extracted text, every passage embedding, all bookmarks, and how far the
    reader had got -- so moving a book you were halfway through would quietly
    reset it to page one.

    The cover cache is keyed by a hash of rel_path, so it is renamed too rather
    than orphaned and regenerated.
    """
    db = _db()
    row = db.execute("SELECT cover FROM books WHERE rel_path=?", (old_rel,)).fetchone()
    if not row:
        return False

    new_cover = ""
    if row["cover"]:
        new_cover = bi.cover_cache_name(new_rel)
        try:
            os.replace(os.path.join(_cache_dir(), row["cover"]),
                       os.path.join(_cache_dir(), new_cover))
        except OSError:
            new_cover = ""

    for t in ("books", "book_authors", "book_sections", "book_chunks",
              "book_progress", "book_bookmarks"):
        db.execute(f"UPDATE {t} SET rel_path=? WHERE rel_path=?", (new_rel, old_rel))
    db.execute("UPDATE books SET cover=?, sort_title=sort_title WHERE rel_path=?",
               (new_cover, new_rel))
    # Triage decisions follow the file too, so a moved-then-rescanned book isn't
    # re-asked about.
    db.execute("UPDATE book_triage SET rel_path=? WHERE rel_path=?",
               (new_rel, old_rel))
    db.commit()
    return True


def sha_exists(sha: str) -> str | None:
    """rel_path of a book whose content hash matches, or None.

    Books aren't in the `files` table, so manager's image dedup query can never
    see them. Re-uploading the same epub from a second device is the single most
    common way a book library grows duplicates, so it is worth catching.

    Hashes are computed lazily and cached on the row: hashing 3000 books at
    index time would add minutes to a scan for a check most people never
    trigger, and hashing on demand amortises it across uploads.
    """
    db = _db()
    r = db.execute("SELECT rel_path FROM books WHERE sha256=?", (sha,)).fetchone()
    if r:
        return r["rel_path"]
    # Fill in any missing hashes, newest first, then re-check once.
    rows = db.execute("SELECT rel_path FROM books WHERE sha256 IS NULL OR sha256=''"
                      ).fetchall()
    if not rows:
        return None
    for rr in rows:
        ap = _abs(rr["rel_path"])
        if not ap or not os.path.exists(ap):
            continue
        h = _sha256_file(ap)
        db.execute("UPDATE books SET sha256=? WHERE rel_path=?", (h, rr["rel_path"]))
        if h == sha:
            db.commit()
            return rr["rel_path"]
    db.commit()
    return None


def _sha256_file(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def delete_book(rel_path: str, remove_file: bool = True) -> bool:
    """Delete a book: its rows, its cover cache, and optionally the file."""
    ap = _abs(rel_path)
    _purge_book(rel_path)
    if remove_file and ap and os.path.exists(ap):
        try:
            os.remove(ap)
        except OSError as e:
            _log().error(f"book delete {rel_path}: {e}")
            return False
    return True


def _purge_book(rel_path):
    db = _db()
    row = db.execute("SELECT cover FROM books WHERE rel_path=?", (rel_path,)).fetchone()
    if row and row["cover"]:
        try:
            os.remove(os.path.join(_cache_dir(), row["cover"]))
        except OSError:
            pass
    for t in ("books", "book_authors", "book_sections", "book_chunks",
              "book_progress", "book_bookmarks"):
        db.execute(f"DELETE FROM {t} WHERE rel_path=?", (rel_path,))
    db.commit()


# ══════════════════════════════════════════════════════════════════════════════
# Text extraction
# ══════════════════════════════════════════════════════════════════════════════

def _extract_one(rel_path) -> str:
    db = _db()
    row = db.execute("SELECT fmt, reader FROM books WHERE rel_path=?",
                     (rel_path,)).fetchone()
    if not row:
        return "missing"
    ap = _abs(rel_path)
    if not ap or not os.path.exists(ap):
        return "missing"
    if row["reader"] == "paged":
        db.execute("UPDATE books SET text_status='unsupported', "
                   "text_error='paged format — pages render on demand' "
                   "WHERE rel_path=?", (rel_path,))
        db.commit()
        return "unsupported"

    res = bi.extract_sections(ap, row["fmt"])
    db.execute("DELETE FROM book_sections WHERE rel_path=?", (rel_path,))
    if res.status == "ok":
        for i, sec in enumerate(res.sections):
            db.execute("INSERT INTO book_sections(rel_path,idx,title,html,chars) "
                       "VALUES(?,?,?,?,?)",
                       (rel_path, i, sec.get("title", ""), sec["html"],
                        len(sec["html"])))
        db.execute("UPDATE books SET text_status='ok', text_error='', "
                   "word_count=?, page_count=COALESCE(page_count,?) WHERE rel_path=?",
                   (res.word_count, max(1, res.word_count // 300), rel_path))
    else:
        db.execute("UPDATE books SET text_status=?, text_error=? WHERE rel_path=?",
                   (res.status, res.error[:500], rel_path))
    db.commit()
    return res.status


def _extract_background(force=False):
    if book_state["extracting"]:
        return
    book_state["extracting"] = True
    book_state["status"] = "extracting"
    try:
        db = _db()
        if force:
            rows = db.execute("SELECT rel_path FROM books WHERE reader='flow'").fetchall()
        else:
            rows = db.execute(
                "SELECT rel_path FROM books WHERE reader='flow' AND "
                "text_status IN ('pending','failed')").fetchall()
        book_state["ext_total"] = len(rows)
        book_state["ext_done"] = 0
        for r in rows:
            try:
                _extract_one(r["rel_path"])
            except Exception as e:
                _log().error(f"book extract {r['rel_path']}: {e}")
            book_state["ext_done"] += 1
        book_state["status"] = "idle"
    finally:
        book_state["extracting"] = False


# ══════════════════════════════════════════════════════════════════════════════
# Comic pages — panel detection + OCR
# ══════════════════════════════════════════════════════════════════════════════

def _page_row(rel_path, n):
    return _db().execute("SELECT * FROM book_pages WHERE rel_path=? AND page=?",
                         (rel_path, n)).fetchone()


def _save_page(rel_path, n, res):
    _db().execute("""
        INSERT INTO book_pages(rel_path,page,w,h,panels,lines,text,
                               panel_src,engine,rtl,updated)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(rel_path,page) DO UPDATE SET
            w=excluded.w, h=excluded.h, panels=excluded.panels,
            lines=excluded.lines, text=excluded.text,
            panel_src=excluded.panel_src, engine=excluded.engine,
            rtl=excluded.rtl, updated=excluded.updated
    """, (rel_path, n, res.get("w", 0), res.get("h", 0),
          json.dumps(res.get("panels", [])), json.dumps(res.get("lines", [])),
          res.get("text", ""), res.get("panel_src", ""), res.get("engine", ""),
          1 if res.get("rtl") else 0, time.time()))
    _db().commit()


def _comic_background(rel_path, do_panels, do_ocr, force, rtl, per_panel):
    """Analyse every page of one comic.

    Runs page by page and commits each result as it lands. That matters more
    than it looks: these jobs take minutes to hours, and a crash or a restart at
    page 280 should cost one page, not the whole run. It also means the reader
    can show overlays for the pages already done while the rest is still going.
    """
    import comic_pages as cp
    if book_state["comic"]:
        return
    _comic_cancel.clear()
    book_state.update(comic=True, comic_done=0, comic_total=0,
                      comic_book=rel_path, status="comic",
                      comic_stage="panels+ocr" if (do_panels and do_ocr)
                      else ("panels" if do_panels else "ocr"))
    try:
        row = _db().execute("SELECT fmt, reader, page_count FROM books "
                            "WHERE rel_path=?", (rel_path,)).fetchone()
        if not row or row["reader"] != "paged":
            book_state["last_error"] = "Not a paged book."
            return
        ap = _abs(rel_path)
        if not ap or not os.path.exists(ap):
            book_state["last_error"] = "File missing."
            return

        fmt = row["fmt"]
        # List the archive once. Reopening it per page turns an O(n) job into
        # O(n^2) on formats where listing means scanning the container.
        names = None if fmt == "pdf" else bi.comic_page_names(ap, fmt)
        total = (row["page_count"] or 0) if fmt == "pdf" else len(names or [])
        if not total:
            book_state["last_error"] = (
                f"No pages readable from this {fmt} — rarfile / py7zr may be missing.")
            return
        book_state["comic_total"] = total

        panel_fn = CTX.get("panel_fn") if do_panels else None
        ocr_fn = CTX.get("ocr_fn") if do_ocr else None
        if do_ocr and ocr_fn is None:
            book_state["last_error"] = "No OCR engine wired up."
            return

        for n in range(total):
            if _comic_cancel.is_set():
                book_state["last_error"] = f"Cancelled at page {n} of {total}."
                break
            try:
                prev = _page_row(rel_path, n)
                if prev and not force:
                    # Already have what's being asked for? Skip the page.
                    have_p = bool(json.loads(prev["panels"] or "[]"))
                    have_o = bool(prev["engine"])
                    if (not do_panels or have_p) and (not do_ocr or have_o):
                        book_state["comic_done"] = n + 1
                        continue
                bgr = cp.page_bgr(ap, fmt, n, page_names=names)
                if bgr is None:
                    book_state["comic_done"] = n + 1
                    continue
                # An OCR-only re-run reuses panels found earlier rather than
                # paying for detection twice.
                known = None
                page_rtl = rtl
                if not do_panels and prev:
                    known = json.loads(prev["panels"] or "[]")
                    # Reading direction belongs to the panels, not to this run.
                    # Without this an OCR pass over a manga silently reorders it
                    # left-to-right, and the transcript comes out scrambled with
                    # nothing on screen to explain why.
                    page_rtl = bool(prev["rtl"])
                res = cp.analyze_page(
                    bgr, panel_fn=panel_fn, ocr_fn=ocr_fn,
                    do_panels=do_panels, do_ocr=do_ocr,
                    rtl=page_rtl, per_panel=per_panel, known_panels=known)
                if not do_panels and prev:
                    # Preserve the stored panels; analyze_page echoed them back
                    # but didn't re-derive them.
                    res["panel_src"] = prev["panel_src"] or "cached"
                if not do_ocr and prev:
                    res["lines"] = json.loads(prev["lines"] or "[]")
                    res["text"] = prev["text"] or ""
                    res["engine"] = prev["engine"] or ""
                _save_page(rel_path, n, res)
            except Exception as e:
                _log().error(f"comic page {rel_path}#{n}: {e}")
            book_state["comic_done"] = n + 1
        book_state["status"] = "idle"
    except Exception as e:
        book_state["last_error"] = str(e)
        _log().error(f"comic job {rel_path}: {e}")
    finally:
        book_state.update(comic=False, comic_book="", comic_stage="")
        CTX.get("db_close", lambda: None)()


# ══════════════════════════════════════════════════════════════════════════════
# Embeddings
# ══════════════════════════════════════════════════════════════════════════════

def _embed_background(force=False):
    if book_state["embedding"]:
        return
    if not CTX["embed_enabled"]():
        book_state["last_error"] = ("No OAI embedding model configured — set one "
                                    "in Settings to enable passage search.")
        return
    book_state["embedding"] = True
    book_state["status"] = "embedding"
    sig = bi.emb_sig(CTX["embed_tag"]())
    try:
        db = _db()
        if force:
            db.execute("DELETE FROM book_chunks")
            db.commit()
        rows = db.execute(
            "SELECT rel_path FROM books WHERE text_status='ok' AND "
            "(emb_status!='ok' OR emb_status IS NULL)").fetchall()
        book_state["emb_total"] = len(rows)
        book_state["emb_done"] = 0
        for r in rows:
            rp = r["rel_path"]
            try:
                _embed_one(rp, sig)
            except Exception as e:
                _log().error(f"book embed {rp}: {e}")
            book_state["emb_done"] += 1
        book_state["status"] = "idle"
    finally:
        book_state["embedding"] = False


def _embed_one(rel_path, sig):
    db = _db()
    secs = db.execute(
        "SELECT idx, title, html FROM book_sections WHERE rel_path=? ORDER BY idx",
        (rel_path,)).fetchall()
    if not secs:
        return
    chunks = bi.chunk_sections([{"html": s["html"]} for s in secs])
    db.execute("DELETE FROM book_chunks WHERE rel_path=?", (rel_path,))
    embed = CTX["embed_text"]
    for i, c in enumerate(chunks):
        vec = embed(c["text"])
        db.execute("INSERT INTO book_chunks(rel_path,idx,section,offset,text,emb,emb_sig) "
                   "VALUES(?,?,?,?,?,?,?)",
                   (rel_path, i, c["section"], c["offset"], c["text"],
                    bi.pack_emb(vec) if vec is not None else None, sig))
    db.execute("UPDATE books SET emb_status='ok' WHERE rel_path=?", (rel_path,))
    db.commit()


# ══════════════════════════════════════════════════════════════════════════════
# Row shaping
# ══════════════════════════════════════════════════════════════════════════════

def _row_dict(r):
    return {
        "rel_path": r["rel_path"], "fmt": r["fmt"], "kind": r["kind"],
        "reader": r["reader"], "title": r["title"],
        "authors": json.loads(r["authors"] or "[]"),
        "series": r["series"], "series_index": r["series_index"],
        "publisher": r["publisher"], "published": r["published"],
        "language": r["language"], "isbn": r["isbn"],
        "identifiers": json.loads(r["identifiers"] or "{}"),
        "description": r["description"],
        "subjects": json.loads(r["subjects"] or "[]"),
        "tags": json.loads(r["tags"] or "[]"),
        "rating": r["rating"], "page_count": r["page_count"],
        "word_count": r["word_count"], "size": r["size"],
        "has_cover": bool(r["cover"]),
        "text_status": r["text_status"], "text_error": r["text_error"],
        "source": r["source"],
    }


# ══════════════════════════════════════════════════════════════════════════════
# register()
# ══════════════════════════════════════════════════════════════════════════════

def register(app, ctx: dict):
    CTX.clear()
    CTX.update(ctx)

    try:
        bi.ensure_tables(ctx["db"]())
    except Exception as e:
        ctx["logger"].error(f"book ensure_tables: {e}")

    # ── status / workers ─────────────────────────────────────────────────────
    @app.route("/api/books/status")
    def books_status():
        db = _db()
        c = db.execute(
            "SELECT COUNT(*) tot, "
            "SUM(CASE WHEN kind='comic' THEN 1 ELSE 0 END) comics, "
            "SUM(CASE WHEN text_status='ok' THEN 1 ELSE 0 END) extracted, "
            "SUM(CASE WHEN text_status='needs_backend' THEN 1 ELSE 0 END) blocked, "
            "SUM(CASE WHEN emb_status='ok' THEN 1 ELSE 0 END) embedded "
            "FROM books").fetchone()
        authors = db.execute(
            "SELECT COUNT(DISTINCT author) n FROM book_authors").fetchone()["n"]
        series = db.execute(
            "SELECT COUNT(DISTINCT series) n FROM books WHERE series!=''").fetchone()["n"]
        pending_triage = db.execute(
            "SELECT COUNT(*) n FROM book_triage WHERE decision IS NULL").fetchone()["n"]
        return jsonify({
            "success": True, "state": book_state,
            "books": c["tot"] or 0, "comics": c["comics"] or 0,
            "extracted": c["extracted"] or 0, "blocked": c["blocked"] or 0,
            "embedded": c["embedded"] or 0,
            "authors": authors, "series": series, "triage": pending_triage,
            "calibre": bi.have_calibre(),
            "search_ready": bool(ctx["embed_enabled"]()),
        })

    @app.route("/api/books/reindex", methods=["POST"])
    def books_reindex():
        force = bool((request.json or {}).get("force"))
        threading.Thread(target=_index_background, args=(force,), daemon=True).start()
        return jsonify({"success": True})

    @app.route("/api/books/extract", methods=["POST"])
    def books_extract():
        d = request.json or {}
        if d.get("rel_path"):
            return jsonify({"success": True, "status": _extract_one(d["rel_path"])})
        threading.Thread(target=_extract_background, args=(bool(d.get("force")),),
                         daemon=True).start()
        return jsonify({"success": True})

    @app.route("/api/books/embed", methods=["POST"])
    def books_embed():
        force = bool((request.json or {}).get("force"))
        if not ctx["embed_enabled"]():
            return jsonify({"success": False,
                            "error": "Set an OAI embedding model in Settings first."})
        threading.Thread(target=_embed_background, args=(force,), daemon=True).start()
        return jsonify({"success": True})

    # ── browsing ─────────────────────────────────────────────────────────────
    @app.route("/api/books/list")
    def books_list():
        a = request.args
        clauses, params = [], []
        if a.get("author"):
            clauses.append("rel_path IN (SELECT rel_path FROM book_authors WHERE author=?)")
            params.append(a["author"])
        if a.get("series"):
            clauses.append("series=?")
            params.append(a["series"])
        if a.get("kind"):
            clauses.append("kind=?")
            params.append(a["kind"])
        if a.get("fmt"):
            clauses.append("fmt=?")
            params.append(a["fmt"])
        if a.get("folder"):
            clauses.append("rel_path LIKE ?")
            params.append(a["folder"].rstrip("/") + "/%")
        q = (a.get("q") or "").strip()
        if q:
            like = f"%{q}%"
            clauses.append("(title LIKE ? OR authors LIKE ? OR series LIKE ? "
                           "OR description LIKE ? OR tags LIKE ? OR subjects LIKE ?)")
            params += [like] * 6
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        sort = {"title": "sort_title COLLATE NOCASE",
                "added": "added DESC",
                "series": "series COLLATE NOCASE, series_index, sort_title",
                "author": "sort_title COLLATE NOCASE",
                "size": "size DESC"}.get(a.get("sort", "title"),
                                         "sort_title COLLATE NOCASE")
        page = max(0, int(a.get("page", 0)))
        per = min(500, int(a.get("per", 120)))
        db = _db()
        total = db.execute(f"SELECT COUNT(*) FROM books{where}", params).fetchone()[0]
        rows = db.execute(
            f"SELECT * FROM books{where} ORDER BY {sort} LIMIT ? OFFSET ?",
            (*params, per, page * per)).fetchall()
        return jsonify({"success": True, "total": total, "page": page,
                        "page_size": per, "books": [_row_dict(r) for r in rows]})

    @app.route("/api/books/authors")
    def books_authors():
        rows = _db().execute("""
            SELECT ba.author AS name, COUNT(*) AS books
            FROM book_authors ba GROUP BY ba.author
            ORDER BY ba.author COLLATE NOCASE""").fetchall()
        return jsonify({"success": True, "authors": [dict(r) for r in rows]})

    @app.route("/api/books/series")
    def books_series():
        rows = _db().execute("""
            SELECT series AS name, COUNT(*) AS books, MIN(published) AS started
            FROM books WHERE series!='' GROUP BY series
            ORDER BY series COLLATE NOCASE""").fetchall()
        return jsonify({"success": True, "series": [dict(r) for r in rows]})

    @app.route("/api/books/detail")
    def books_detail():
        rp = request.args.get("rel_path", "")
        r = _db().execute("SELECT * FROM books WHERE rel_path=?", (rp,)).fetchone()
        if not r:
            return jsonify({"success": False, "error": "not found"}), 404
        d = _row_dict(r)
        prog = _db().execute(
            "SELECT locator, percent FROM book_progress WHERE rel_path=? AND user=?",
            (rp, _user())).fetchone()
        d["progress"] = dict(prog) if prog else {"locator": "", "percent": 0}
        d["sections"] = _db().execute(
            "SELECT COUNT(*) n FROM book_sections WHERE rel_path=?", (rp,)
        ).fetchone()["n"]
        return jsonify({"success": True, "book": d})

    @app.route("/api/books/meta", methods=["POST"])
    def books_meta():
        d = request.json or {}
        rp = d.get("rel_path", "")
        if not _db().execute("SELECT 1 FROM books WHERE rel_path=?", (rp,)).fetchone():
            return jsonify({"success": False, "error": "not found"}), 404
        sets, params = [], []
        for k in ("title", "series", "publisher", "published", "language",
                  "isbn", "description", "source"):
            if k in d:
                sets.append(f"{k}=?")
                params.append(d[k] or "")
        if "title" in d:
            sets.append("sort_title=?")
            params.append(bi.sort_title(d["title"] or ""))
        if "series_index" in d:
            sets.append("series_index=?")
            try:
                params.append(float(d["series_index"]))
            except (TypeError, ValueError):
                params.append(None)
        if "rating" in d:
            sets.append("rating=?")
            params.append(int(d["rating"] or 0))
        for k in ("tags", "subjects", "identifiers"):
            if k in d:
                sets.append(f"{k}=?")
                params.append(json.dumps(d[k]))
        db = _db()
        if "authors" in d:
            authors = [a.strip() for a in (d["authors"] or []) if a.strip()]
            sets.append("authors=?")
            params.append(json.dumps(authors))
            db.execute("DELETE FROM book_authors WHERE rel_path=?", (rp,))
            for a in authors:
                db.execute("INSERT OR IGNORE INTO book_authors(rel_path,author) "
                           "VALUES(?,?)", (rp, a))
        if sets:
            params.append(rp)
            db.execute(f"UPDATE books SET {','.join(sets)} WHERE rel_path=?", params)
        db.commit()
        return jsonify({"success": True})

    # ── assets ───────────────────────────────────────────────────────────────
    @app.route("/api/books/cover/<path:rel_path>")
    def books_cover(rel_path):
        r = _db().execute("SELECT cover FROM books WHERE rel_path=?",
                          (rel_path,)).fetchone()
        if not r or not r["cover"]:
            return jsonify({"success": False, "error": "no cover"}), 404
        p = os.path.join(_cache_dir(), r["cover"])
        if not os.path.exists(p):
            return jsonify({"success": False, "error": "no cover"}), 404
        return send_file(p, mimetype="image/jpeg", conditional=True)

    @app.route("/api/books/toc/<path:rel_path>")
    def books_toc(rel_path):
        rows = _db().execute(
            "SELECT idx, title, chars FROM book_sections WHERE rel_path=? ORDER BY idx",
            (rel_path,)).fetchall()
        return jsonify({"success": True, "toc": [dict(r) for r in rows]})

    @app.route("/api/books/section/<path:rel_path>")
    def books_section(rel_path):
        idx = int(request.args.get("idx", 0))
        r = _db().execute(
            "SELECT idx, title, html FROM book_sections WHERE rel_path=? AND idx=?",
            (rel_path, idx)).fetchone()
        if not r:
            # Lazy first-read extraction: opening a book the batch job hasn't
            # reached yet should just work, not show an empty reader.
            status = _extract_one(rel_path)
            if status != "ok":
                row = _db().execute("SELECT text_status, text_error FROM books "
                                    "WHERE rel_path=?", (rel_path,)).fetchone()
                return jsonify({"success": False, "status": status,
                                "error": (row["text_error"] if row else "")}), 404
            r = _db().execute(
                "SELECT idx, title, html FROM book_sections WHERE rel_path=? AND idx=?",
                (rel_path, idx)).fetchone()
            if not r:
                return jsonify({"success": False, "error": "no such section"}), 404
        total = _db().execute("SELECT COUNT(*) n FROM book_sections WHERE rel_path=?",
                              (rel_path,)).fetchone()["n"]
        return jsonify({"success": True, "idx": r["idx"], "title": r["title"],
                        "html": r["html"], "total": total})

    @app.route("/api/books/page/<path:rel_path>")
    def books_page(rel_path):
        """One page of a paged book (PDF / cb*) as an image."""
        n = int(request.args.get("n", 0))
        row = _db().execute("SELECT fmt, reader FROM books WHERE rel_path=?",
                            (rel_path,)).fetchone()
        if not row or row["reader"] != "paged":
            return jsonify({"success": False, "error": "not a paged book"}), 400
        ap = _abs(rel_path)
        if not ap or not os.path.exists(ap):
            return jsonify({"success": False, "error": "file missing"}), 404
        fmt = row["fmt"]
        if fmt == "pdf":
            dpi = max(72, min(300, int(request.args.get("dpi", 150))))
            data = bi.render_pdf_page(ap, n, dpi=dpi)
            if data is None:
                return jsonify({"success": False,
                                "error": "PDF rendering needs PyMuPDF "
                                         "(pip install pymupdf)"}), 501
            return Response(data, mimetype="image/jpeg")
        names = bi.comic_page_names(ap, fmt)
        if not names:
            return jsonify({"success": False,
                            "error": f"cannot read {fmt} archive — "
                                     f"install rarfile/py7zr (or unrar/7z)"}), 501
        if n < 0 or n >= len(names):
            return jsonify({"success": False, "error": "page out of range"}), 404
        data = bi.comic_page_bytes(ap, fmt, names[n])
        if data is None:
            return jsonify({"success": False, "error": "page read failed"}), 500
        ext = os.path.splitext(names[n])[1].lower()
        mime = {".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
                ".jxl": "image/jxl", ".avif": "image/avif"}.get(ext, "image/jpeg")
        return Response(data, mimetype=mime)

    # ── comic pages: panels + OCR ────────────────────────────────────────────
    @app.route("/api/books/comic/analyze", methods=["POST"])
    def books_comic_analyze():
        """Kick off panel detection and/or OCR over a comic's pages.

        `mode` is 'panels', 'ocr' or 'both'. OCR without panels is legal and
        reuses whatever panels are already stored, so the usual flow — detect
        panels, eyeball a page, then OCR — doesn't redetect.
        """
        d = request.json or {}
        rp = d.get("rel_path", "")
        mode = d.get("mode", "both")
        if book_state["comic"]:
            busy = book_state["comic_book"] or "another book"
            return jsonify({"success": False,
                            "error": f"Already analysing {busy}. Cancel it first."})
        row = _db().execute("SELECT reader, kind FROM books WHERE rel_path=?",
                            (rp,)).fetchone()
        if not row:
            return jsonify({"success": False, "error": "not found"}), 404
        if row["reader"] != "paged":
            return jsonify({"success": False,
                            "error": "Only paged books (pdf / cb*) have pages to analyse."})
        do_panels = mode in ("panels", "both")
        do_ocr = mode in ("ocr", "both")
        if do_ocr and not CTX.get("ocr_fn"):
            return jsonify({"success": False,
                            "error": "No OCR engine available on the server."})
        threading.Thread(
            target=_comic_background,
            args=(rp, do_panels, do_ocr, bool(d.get("force")),
                  bool(d.get("rtl")), bool(d.get("per_panel"))),
            daemon=True).start()
        return jsonify({"success": True})

    @app.route("/api/books/comic/cancel", methods=["POST"])
    def books_comic_cancel():
        _comic_cancel.set()
        return jsonify({"success": True})

    @app.route("/api/books/comic/page")
    def books_comic_page():
        """Stored analysis for one page — what the reader overlay draws."""
        rp = request.args.get("rel_path", "")
        n = int(request.args.get("n", 0))
        r = _page_row(rp, n)
        if not r:
            return jsonify({"success": True, "analysed": False,
                            "panels": [], "lines": [], "text": ""})
        return jsonify({"success": True, "analysed": True,
                        "page": n, "w": r["w"], "h": r["h"],
                        "panels": json.loads(r["panels"] or "[]"),
                        "lines": json.loads(r["lines"] or "[]"),
                        "text": r["text"] or "",
                        "panel_src": r["panel_src"], "engine": r["engine"],
                        "rtl": bool(r["rtl"])})

    @app.route("/api/books/comic/summary")
    def books_comic_summary():
        """How much of this comic has been analysed, for the controls pane."""
        rp = request.args.get("rel_path", "")
        c = _db().execute(
            "SELECT COUNT(*) pages, "
            "SUM(CASE WHEN panels!='[]' THEN 1 ELSE 0 END) with_panels, "
            "SUM(CASE WHEN engine!='' THEN 1 ELSE 0 END) with_ocr, "
            "SUM(CASE WHEN text!='' THEN 1 ELSE 0 END) with_text, "
            "MAX(rtl) rtl "
            "FROM book_pages WHERE rel_path=?", (rp,)).fetchone()
        tot = _db().execute("SELECT page_count FROM books WHERE rel_path=?",
                            (rp,)).fetchone()
        return jsonify({"success": True,
                        "pages": c["pages"] or 0,
                        "with_panels": c["with_panels"] or 0,
                        "with_ocr": c["with_ocr"] or 0,
                        "with_text": c["with_text"] or 0,
                        "page_count": (tot["page_count"] if tot else 0) or 0,
                        "rtl": bool(c["rtl"]),
                        "running": bool(book_state["comic"]),
                        "ocr_available": bool(CTX.get("ocr_fn"))})

    @app.route("/api/books/comic/text")
    def books_comic_text():
        """The whole transcript, in reading order. Also the thing worth feeding
        to an LLM or a search index."""
        rp = request.args.get("rel_path", "")
        rows = _db().execute(
            "SELECT page, text FROM book_pages WHERE rel_path=? AND text!='' "
            "ORDER BY page", (rp,)).fetchall()
        if request.args.get("format") == "txt":
            body = "\n\n".join(f"── page {r['page'] + 1} ──\n{r['text']}"
                               for r in rows)
            return Response(body, mimetype="text/plain; charset=utf-8")
        return jsonify({"success": True,
                        "pages": [{"page": r["page"], "text": r["text"]}
                                  for r in rows]})

    @app.route("/api/books/comic/panels", methods=["POST"])
    def books_comic_set_panels():
        """Replace one page's panels by hand.

        Detection is good, not perfect, and a wrong panel box quietly misfiles
        every OCR line inside it. Letting someone correct a page is cheaper than
        chasing the last few percent of detector accuracy.
        """
        import comic_pages as cp
        d = request.json or {}
        rp = d.get("rel_path", "")
        n = int(d.get("page", 0))
        rtl = bool(d.get("rtl"))
        panels = cp.order_panels([
            {"cx": float(p["cx"]), "cy": float(p["cy"]),
             "w": float(p["w"]), "h": float(p["h"])}
            for p in (d.get("panels") or [])], rtl)
        prev = _page_row(rp, n)
        lines = json.loads(prev["lines"] or "[]") if prev else []
        # Rebind existing OCR lines to the corrected panels — that's the whole
        # point of fixing a box, so it shouldn't need a re-run of OCR.
        for ln in lines:
            ln["panel"] = cp._assign_panel(ln, panels)
        _save_page(rp, n, {
            "w": prev["w"] if prev else 0, "h": prev["h"] if prev else 0,
            "panels": panels, "lines": lines,
            "text": cp.build_text(panels, lines, rtl),
            "panel_src": "manual",
            "engine": prev["engine"] if prev else "", "rtl": rtl})
        return jsonify({"success": True, "panels": panels})

    @app.route("/api/books/download/<path:rel_path>")
    def books_download(rel_path):
        ap = _abs(rel_path)
        if not ap or not os.path.exists(ap):
            return jsonify({"success": False, "error": "not found"}), 404
        return send_file(ap, as_attachment=True, conditional=True)

    # ── reading position ─────────────────────────────────────────────────────
    @app.route("/api/books/progress", methods=["GET", "POST"])
    def books_progress():
        if request.method == "GET":
            rp = request.args.get("rel_path", "")
            r = _db().execute(
                "SELECT locator, percent, updated FROM book_progress "
                "WHERE rel_path=? AND user=?", (rp, _user())).fetchone()
            return jsonify({"success": True,
                            "progress": dict(r) if r else
                            {"locator": "", "percent": 0, "updated": 0}})
        d = request.json or {}
        rp = d.get("rel_path", "")
        _db().execute("""
            INSERT INTO book_progress(rel_path,user,locator,percent,updated)
            VALUES(?,?,?,?,?)
            ON CONFLICT(rel_path,user) DO UPDATE SET
                locator=excluded.locator, percent=excluded.percent,
                updated=excluded.updated
        """, (rp, _user(), str(d.get("locator", "")),
              float(d.get("percent", 0) or 0), time.time()))
        _db().commit()
        return jsonify({"success": True})

    @app.route("/api/books/bookmarks", methods=["GET", "POST", "DELETE"])
    def books_bookmarks():
        db = _db()
        if request.method == "GET":
            rows = db.execute(
                "SELECT * FROM book_bookmarks WHERE rel_path=? AND user=? "
                "ORDER BY created", (request.args.get("rel_path", ""), _user())
            ).fetchall()
            return jsonify({"success": True, "bookmarks": [dict(r) for r in rows]})
        d = request.json or {}
        if request.method == "DELETE":
            db.execute("DELETE FROM book_bookmarks WHERE id=? AND user=?",
                       (int(d.get("id", 0)), _user()))
            db.commit()
            return jsonify({"success": True})
        cur = db.execute(
            "INSERT INTO book_bookmarks(rel_path,user,locator,label,note,created) "
            "VALUES(?,?,?,?,?,?)",
            (d.get("rel_path", ""), _user(), str(d.get("locator", "")),
             d.get("label", ""), d.get("note", ""), time.time()))
        db.commit()
        return jsonify({"success": True, "id": cur.lastrowid})

    # ── search ───────────────────────────────────────────────────────────────
    @app.route("/api/books/search", methods=["POST"])
    def books_search():
        """Passage-level semantic search. Returns matching PASSAGES grouped by
        book, so you land on the page rather than on the cover."""
        d = request.json or {}
        q = (d.get("q") or "").strip()
        limit = min(100, int(d.get("limit", 30)))
        if not q:
            return jsonify({"success": False, "error": "empty query"})
        if not ctx["embed_enabled"]():
            return jsonify({"success": False,
                            "error": "Semantic search needs an OAI embedding "
                                     "model (set it in Settings)."})
        sig = bi.emb_sig(ctx["embed_tag"]())
        db = _db()
        n = db.execute("SELECT COUNT(*) n FROM book_chunks WHERE emb_sig=? "
                       "AND emb IS NOT NULL", (sig,)).fetchone()["n"]
        if n == 0:
            other = db.execute("SELECT DISTINCT emb_sig FROM book_chunks "
                               "LIMIT 1").fetchone()
            if other:
                return jsonify({"success": False,
                                "error": f"Stored passage vectors use "
                                         f"'{other['emb_sig']}', not the current "
                                         f"model. Re-embed to search."})
            return jsonify({"success": False,
                            "error": "No passage embeddings yet — run Embed."})
        qv = ctx["embed_text"](q)
        if qv is None:
            return jsonify({"success": False, "error": "failed to embed the query"})

        rows = db.execute(
            "SELECT rel_path, idx, section, offset, text, emb FROM book_chunks "
            "WHERE emb_sig=? AND emb IS NOT NULL", (sig,)).fetchall()
        ranked = bi.rank_by_vector(qv, rows)

        # Group by book, keep each book's best passages, book score = best chunk.
        by_book: dict = {}
        for score, rp, idx, section, offset, text in ranked:
            b = by_book.setdefault(rp, {"score": score, "passages": []})
            if len(b["passages"]) < 3:
                b["passages"].append({"score": round(score, 4), "section": section,
                                      "offset": offset, "text": text[:400]})
        order = sorted(by_book.items(), key=lambda kv: -kv[1]["score"])[:limit]

        out = []
        for rp, info in order:
            r = db.execute("SELECT * FROM books WHERE rel_path=?", (rp,)).fetchone()
            if not r:
                continue
            e = _row_dict(r)
            e["score"] = round(info["score"], 4)
            e["passages"] = info["passages"]
            out.append(e)
        return jsonify({"success": True, "results": out})

    @app.route("/api/books/delete", methods=["POST"])
    def books_delete():
        """Delete a book. `keep_file` removes it from the library but leaves the
        bytes on disk — useful when the shelf is wrong but the file isn't."""
        d = request.json or {}
        rp = d.get("rel_path", "")
        if not _db().execute("SELECT 1 FROM books WHERE rel_path=?", (rp,)).fetchone():
            return jsonify({"success": False, "error": "not found"}), 404
        ok = delete_book(rp, remove_file=not d.get("keep_file"))
        return jsonify({"success": ok})

    # ── LLM ──────────────────────────────────────────────────────────────────
    @app.route("/api/books/summarize", methods=["POST"])
    def books_summarize():
        """Blurb a book with the configured LLM.

        /api/run_llm can't serve this: it decodes the file as an image first,
        which is exactly the wrong move for an epub. We send TEXT instead — the
        opening and a few sampled passages rather than the whole book, because a
        400k-word novel is not going in a context window and the first chapter
        plus a spread of samples is enough to write a jacket blurb.
        """
        d = request.json or {}
        rp = d.get("rel_path", "")
        llm = ctx.get("llm_request")
        if not llm:
            return jsonify({"success": False, "error": "LLM not wired up"}), 501
        db = _db()
        row = db.execute("SELECT title, authors FROM books WHERE rel_path=?",
                         (rp,)).fetchone()
        if not row:
            return jsonify({"success": False, "error": "not found"}), 404
        secs = db.execute(
            "SELECT html FROM book_sections WHERE rel_path=? ORDER BY idx",
            (rp,)).fetchall()
        if not secs:
            return jsonify({"success": False,
                            "error": "No extracted text — run Extract first."})
        texts = [bi._strip_tags(s["html"]) for s in secs]
        sample = texts[0][:6000]
        step = max(1, len(texts) // 4)
        for t in texts[step::step][:3]:
            sample += "\n\n[…]\n\n" + t[:2000]

        prompt = (
            f"Below are excerpts from a book titled {row['title']!r} by "
            f"{', '.join(json.loads(row['authors'] or '[]')) or 'an unknown author'}.\n"
            "Write a 3-5 sentence jacket blurb: what it is about, its tone, and "
            "who would enjoy it. No spoilers past the opening act. Reply with the "
            "blurb only — no preamble, no headings.\n\n"
            f"{sample[:14000]}"
        )
        try:
            msg = llm([{"role": "user", "content": prompt}], timeout=300)
            text = (msg.get("content") or "").strip()
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})
        if not text:
            return jsonify({"success": False, "error": "empty response"})
        db.execute("UPDATE books SET description=? WHERE rel_path=?", (text, rp))
        db.commit()
        return jsonify({"success": True, "description": text})

    # ── triage ───────────────────────────────────────────────────────────────
    @app.route("/api/books/triage")
    def books_triage():
        rows = _db().execute(
            "SELECT * FROM book_triage WHERE decision IS NULL "
            "ORDER BY size DESC LIMIT 500").fetchall()
        return jsonify({"success": True, "items": [dict(r) for r in rows]})

    @app.route("/api/books/triage/decide", methods=["POST"])
    def books_triage_decide():
        d = request.json or {}
        rp = d.get("rel_path", "")
        decision = d.get("decision", "")
        if decision not in ("book", "not_book"):
            return jsonify({"success": False, "error": "bad decision"}), 400
        db = _db()
        db.execute("UPDATE book_triage SET decision=?, decided=? WHERE rel_path=?",
                   (decision, time.time(), rp))
        db.commit()
        if decision == "book":
            ap = _abs(rp)
            if ap and os.path.exists(ap):
                fmt = d.get("fmt") or bi.sniff(ap) or "text"
                v = bi.Verdict("book", fmt, d.get("kind", "book"), "user decision")
                _upsert_book(rp, ap, v, force=True)
        else:
            _purge_book(rp)
        return jsonify({"success": True})

    @app.route("/api/books/triage/decide_all", methods=["POST"])
    def books_triage_decide_all():
        """Bulk-answer every pending item sharing an extension + reason. With
        thousands of ao3 dumps the queue is repetitive by nature; one click
        should clear a whole class."""
        d = request.json or {}
        ext = d.get("ext", "")
        reason = d.get("reason", "")
        decision = d.get("decision", "")
        if decision not in ("book", "not_book"):
            return jsonify({"success": False, "error": "bad decision"}), 400
        rows = _db().execute(
            "SELECT rel_path, sniffed FROM book_triage WHERE decision IS NULL "
            "AND ext=? AND reason=?", (ext, reason)).fetchall()
        for r in rows:
            _db().execute("UPDATE book_triage SET decision=?, decided=? "
                          "WHERE rel_path=?", (decision, time.time(), r["rel_path"]))
            if decision == "book":
                ap = _abs(r["rel_path"])
                if ap and os.path.exists(ap):
                    v = bi.Verdict("book", r["sniffed"] or "text", "book",
                                   "bulk user decision")
                    try:
                        _upsert_book(r["rel_path"], ap, v, force=True)
                    except Exception as e:
                        _log().error(f"bulk triage {r['rel_path']}: {e}")
        _db().commit()
        return jsonify({"success": True, "count": len(rows)})

    return app


def start_background(force=False):
    """Called from manager.py's __main__ block, mirroring the music indexer."""
    threading.Thread(target=_index_background, args=(force,), daemon=True).start()