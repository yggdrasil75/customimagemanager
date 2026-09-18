"""
Comics module — folder comics and comic archives.
======================================================================
Two shapes of the same thing:
  * a folder of page images with a comic.json (the `comics` table and
    files.comic_folder are caches): create / edit / delete / read in the
    comic modal, page-wise Smart Tag pipeline, box-all;
  * cbz / cbr / cb7 / cbt archives, which stay books (the books module
    shelves and reads them) but whose extensions, mimes and page rendering /
    panel analysis are owned here (comic_pages.py via the books module's
    'book_archive' service).
"""
from . import comics_core as cc
from . import comic_pages as cp

MANIFEST = {
    "id":          "comics",
    "name":        "Comics",
    "version":     "1.0.0",
    "description": "Folder comics (comic.json, reader modal, page pipeline) and comic "
                   "archive (cbz/cbr/cb7) page rendering for the books shelf.",
    "core":        False,
    "requires":    ["books"],
    "pip":         [],
    "assets":      ["comic.js"],
}

_DDL = """
-- A comic is a folder of ordered page images plus its own metadata.
-- Source of truth is <folder>/comic.json (portable); this is a cache.
CREATE TABLE IF NOT EXISTS comics (
    folder      TEXT PRIMARY KEY,
    title       TEXT,
    author      TEXT,
    description TEXT,
    tags        TEXT,
    characters  TEXT,
    cover       TEXT,
    page_order  TEXT,
    created     REAL,
    mtime       REAL
);
"""

_ARCHIVE_EXTS = [".cbz", ".cbr", ".cb7", ".cbt", ".cba"]
_MIME = {".cbz": "application/vnd.comicbook+zip", ".cbr": "application/vnd.comicbook-rar",
         ".cb7": "application/x-cb7", ".cbt": "application/x-cbt"}


def _migrate(db):
    try:
        db.execute("ALTER TABLE files ADD COLUMN comic_folder TEXT DEFAULT ''")
        db.commit()
    except Exception:
        pass


def register(host):
    cc._bind(host)
    cp.BOOKS = host.get_service("book_archive")
    host.add_table(_DDL, check=_migrate)
    host.extend_media_type("book", exts=_ARCHIVE_EXTS, mime_map=_MIME)
    for key, label in (("comics.make", "Make / create comic"), ("comics.edit", "Edit comic pages"),
                       ("comics.delete", "Delete comic")):
        host.register_feature(key, label, section="comics", section_label="Comics", default="write",
                              role_defaults={"viewer": "block"})
    for rule, fn, opts in cc._ROUTES:
        feat = getattr(fn, "_feature", None)
        view = host.core.auth.require_feature(*feat[0], **feat[1])(fn) if feat else fn
        host.add_route(rule, view, **opts)
    host.add_asset("comic.js")
    host.register_app_modal("comic_modal.html")

    # Pages of a comic folder never appear in the flat gallery / folder counts.
    host.register_gallery_filter("(comic_folder IS NULL OR comic_folder='')")

    host.on("library.reconcile", lambda: cc._scan_comics())

    def _joined(rel_path):
        folder = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
        if folder and cc._load_comic_json(folder) is not None:
            cc._set_comic_membership(folder)
    host.on("upload.stored", lambda rel_path, filename: _joined(rel_path))
    host.on("file.renamed", lambda old_rel, new_rel: _joined(new_rel))

    host.provide_service("comic_pages", {
        "page_bgr": cp.page_bgr, "analyze_page": cp.analyze_page,
        "order_panels": cp.order_panels, "assign_panel": cp._assign_panel,
        "build_text": cp.build_text})
    host.provide_service("comics", {"folders": cc._comic_folder_set,
                                    "load": cc._load_comic_json, "pages": cc._comic_ordered_pages})
    host.logger.info("comics module: registered folder comics + archive page rendering")
