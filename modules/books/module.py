"""
Books module — ebook/comic library: reader, shelf, search.
======================================================================
book_routes.py was already a hand-wired register(app, ctx) module (the
comment in manager even said so); this wraps it as a real module. It:
  - registers the Books LEFT TAB (books_pane) via registerLeftTab,
  - contributes the reader + triage modal as app modals, the shelf as the
    left-pane content, and the book controls as a controls-pane partial,
  - serves books.js + reader.js as assets,
  - registers the 'books' auth feature (read=view, write=delete),
  - calls book_routes.register(app, ctx) with a ctx built from the host,
  - subscribes to core events (library.reconcile, upload.duplicate_check,
    upload.stored, file.renamed) so the shelf tracks the library without
    core knowing books exist.

book_index.py is now part of this module (modules/books/book_index.py) and
aliased as `book_index` via modules/__init__.py for upload.py / media_types.py
/ comic_pages.py compatibility.
"""

from . import book_routes
from . import book_index as bi

MANIFEST = {
    "id":          "books",
    "name":        "Books & comics",
    "version":     "1.0.0",
    "description": "Ebook / comic library: reader, shelf, passage search, "
                   "triage. Adds the Books tab.",
    "core":        False,
    "requires":    [],
    "pip":         [],
    "assets":      ["books.js", "reader.js"],
}


def _no_llm(*a, **k):
    raise RuntimeError("LLM not available (vlm module disabled)")


def register(host):
    # Teach core what a "book" is. Without this the app is a pure image gallery
    # that never sees an epub/cbz. The ext lists + mime map live with the module.
    _BOOK_MIME = {
        '.epub': 'application/epub+zip', '.pdf': 'application/pdf',
        '.mobi': 'application/x-mobipocket-ebook',
        '.azw': 'application/vnd.amazon.ebook', '.azw3': 'application/vnd.amazon.ebook',
        '.kf8': 'application/vnd.amazon.ebook', '.kfx': 'application/vnd.amazon.ebook',
        '.fb2': 'application/x-fictionbook+xml', '.lit': 'application/x-ms-reader',
        '.chm': 'application/vnd.ms-htmlhelp', '.lrf': 'application/x-sony-bbeb',
        '.lrx': 'application/x-sony-bbeb', '.rtf': 'application/rtf',
        '.txt': 'text/plain; charset=utf-8', '.html': 'text/html; charset=utf-8',
    }
    # Comic archives (cbz/cbr/cb7…) are books too, but the comics module owns
    # them: it extends this kind with its extensions and page renderer.
    host.register_media_type(
        "book", exts=bi.BOOK_EXTS - bi.COMIC_ARCHIVE_EXTS,
        unambiguous_exts=bi.UNAMBIGUOUS_BOOK_EXTS - bi.COMIC_ARCHIVE_EXTS,
        uploadable_exts=bi.UPLOADABLE_BOOK_EXTS - bi.COMIC_ARCHIVE_EXTS, mime_map=_BOOK_MIME)

    # Auth feature: read = browse the shelf/read, write = delete/triage.
    host.register_feature("tab.books", "Books tab (read=view, write=delete)",
                          section="gallery_tabs", section_label="Gallery tabs",
                          default="read", role_defaults={"viewer": "read"})

    # Front-end: assets + templates. The reader + triage are top-level modals;
    # the shelf pane and book controls are their own partials (server-rendered).
    host.add_asset("books.js")
    host.add_asset("reader.js")
    host.register_app_modal("book_reader.html")
    host.register_app_modal("book_triage_modal.html")
    host.register_left_pane("books_pane.html")       # left-pane shelf content
    host.register_controls_pane("book", "book_controls.html")

    # The Books LEFT TAB is registered from books.js via window.registerLeftTab
    # (the tab bar is a front-end extension area), so the button + onShow live
    # with the module's JS, not a Python slot.

    # Wire the actual routes via the existing register(app, ctx). ctx is built
    # from the host + a couple of core helpers reached lazily.
    # Embedding functions are now provided by the embedding module's service.
    emb_svc = host.get_service("embedding") or {}
    core = host.core
    book_routes.register(host.app, {
        "db":            host.db,
        "media_dir":     host.media_dir,
        "safe_path":     host.safe_path,
        "logger":        host.logger,
        "auth":          core.auth,
        "media":         host.media,
        "folder_scope_clause": core.folder_scope_clause,
        "table_exists":  core.table_exists,
        "norm_date_literal": core.norm_date_literal,
        "embed_text":    emb_svc.get("oai_embed_text"),
        "embed_enabled": emb_svc.get("oai_embed_enabled"),
        "embed_tag":     emb_svc.get("oai_embed_tag"),
        "llm_request":   lambda *a, **k: (host.get_service("llm") or {}).get("request", _no_llm)(*a, **k),
        "comic_pages":   lambda: host.get_service("comic_pages"),   # comics module, or None
        "current_user":  host.current_user,
    })

    # Archive / PDF page access for the comics module (cbz/cbr/cb7 pages).
    host.provide_service("book_archive", {"comic_page_bytes": bi.comic_page_bytes,
                                          "comic_page_names": bi.comic_page_names,
                                          "render_pdf_page": bi.render_pdf_page})

    # Search: contribute book + comic results to the gallery search.
    host.register_search_provider(book_routes.query_books)
    host.register_search_provider(book_routes.query_comics)

    # Core events: the library scan, uploads and renames don't know about
    # books; they emit, and this module keeps the shelf in step.
    mt = host.media
    host.on("library.reconcile", lambda: book_routes.reconcile())
    host.on("upload.duplicate_check",
            lambda sha, filename: book_routes.sha_exists(sha) if mt.is_book(filename) else None)
    host.on("upload.stored",
            lambda rel_path, filename: book_routes.index_one(rel_path) if mt.is_book(filename) else None)
    host.on("file.renamed",
            lambda old_rel, new_rel: book_routes.rename_book(old_rel, new_rel) if mt.is_book(old_rel) else None)

    # Background indexer, once the server is up.
    host.on_startup(book_routes.start_background)

    host.logger.info("books module: tab + reader + routes + events registered")
