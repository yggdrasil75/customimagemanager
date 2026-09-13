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
  - exposes reconcile / sha_exists / index_one / rename_book as the
    'books' service so core's upload/rename/reconcile paths call it.

book_index.py is now part of this module (modules/books/book_index.py) and
aliased as `book_index` via modules/__init__.py for upload.py / media_types.py
/ comic_pages.py compatibility.
"""

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


def register(host):
    from flask import g
    from . import book_routes
    from . import book_index as bi

    # Teach core what a "book" is. Without this the app is a pure image gallery
    # that never sees an epub/cbz. The ext lists + mime map live with the module.
    import media_types as mt
    _BOOK_MIME = {
        '.epub': 'application/epub+zip', '.pdf': 'application/pdf',
        '.mobi': 'application/x-mobipocket-ebook',
        '.azw': 'application/vnd.amazon.ebook', '.azw3': 'application/vnd.amazon.ebook',
        '.kf8': 'application/vnd.amazon.ebook', '.kfx': 'application/vnd.amazon.ebook',
        '.fb2': 'application/x-fictionbook+xml', '.lit': 'application/x-ms-reader',
        '.chm': 'application/vnd.ms-htmlhelp', '.lrf': 'application/x-sony-bbeb',
        '.lrx': 'application/x-sony-bbeb', '.rtf': 'application/rtf',
        '.cbz': 'application/vnd.comicbook+zip',
        '.cbr': 'application/vnd.comicbook-rar',
        '.cb7': 'application/x-cb7', '.cbt': 'application/x-cbt',
        '.txt': 'text/plain; charset=utf-8', '.html': 'text/html; charset=utf-8',
    }
    mt.register_media_type(
        "book", exts=bi.BOOK_EXTS, unambiguous_exts=bi.UNAMBIGUOUS_BOOK_EXTS,
        uploadable_exts=bi.UPLOADABLE_BOOK_EXTS, mime_map=_BOOK_MIME)

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
    import manager as m
    book_routes.register(host.app, {
        "db":            host.db,
        "media_dir":     host.media_dir,
        "safe_path":     host.safe_path,
        "logger":        host.logger,
        "embed_text":    m._oai_embed_text,
        "embed_enabled": m._oai_embed_enabled,
        "embed_tag":     m._oai_embed_tag,
        "llm_request":   m._llm_request,
        "current_user":  lambda: (getattr(g, "user", None) or {}).get("username", ""),
    })

    # Search: contribute book + comic results to the gallery search. (The query
    # fns still live in manager for now; wrap them so core's search loop is
    # module-driven and drops cleanly when books is disabled.)
    host.register_search_provider(
        lambda t, f, s: m._query_books(t, f, s))
    host.register_search_provider(
        lambda t, f, s: m._query_comics(t, f, s))

    # Service: core's upload/rename/reconcile paths call these.
    host.provide_service("books", {
        "reconcile":   book_routes.reconcile,
        "sha_exists":  book_routes.sha_exists,
        "index_one":   book_routes.index_one,
        "rename_book": book_routes.rename_book,
    })

    # Background indexer, once the server is up.
    host.on_startup(book_routes.start_background)

    host.logger.info("books module: tab + reader + routes + service registered")
