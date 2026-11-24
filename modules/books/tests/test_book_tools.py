"""! @file
@brief Books: stub "COVER" pages are dropped, paged-book pages are cached, other
modules' tools can read the open book, and DOCX is a storage target that skips calibre."""
import io
import zipfile

import pytest
from PIL import Image

from modules.books import book_index as bi, book_routes as br
from modules.books.tests.test_book_uploads import _book, _cbz, _epub, _png, _up, prefs  # noqa: F401
from modules.encoding import convert as C

_PARA = "<p>" + "Words of a real chapter go here. " * 20 + "</p>"


# -- stub cover pages --------------------------------------------------------------
@pytest.mark.parametrize("html,stub", [
    ("<div><h1>COVER</h1></div>", True),
    ("  <p> Cover </p>", True),
    ("<div></div>", True),
    ('<div><img src="__epub__/c.jpg" alt=""></div>', False),   # a real cover picture
    ("<h1>Cover</h1><p>The cover story begins here.</p>", False),
])
def test_placeholder_section(html, stub):
    assert bi._placeholder_section(html) is stub


def test_epub_cover_stub_is_dropped(tmp_path):
    data = _epub(['<html><head><title>Cover</title></head><body><h1>COVER</h1></body></html>',
                  '<html><head><title>Cover</title></head><body><img src="c.png"/></body></html>',
                  f'<html><body><h1>One</h1>{_PARA}</body></html>'], {"c.png": _png(3)})
    p = tmp_path / "b.epub"
    p.write_bytes(data)
    res = bi._extract_epub(str(p))
    assert res.status == "ok"
    assert len(res.sections) == 2, [s["title"] for s in res.sections]
    assert "<img" in res.sections[0]["html"]            # the picture cover stays
    assert res.sections[1]["title"] == "One"


# -- content for other modules' tools (the books service) -------------------------
def test_content_service_text_book(client, upload, prefs, app):
    prefs(target=".epub", mode="none")
    rel = _up(upload, _epub([f'<html><body><h1>One</h1>{_PARA}</body></html>']), "readme_book.epub")
    assert _book(client, rel)
    client.post("/api/books/meta", json={"rel_path": rel, "tags": ["Space Opera"]})
    svc = app.module_host.get_service("books")
    c = svc["content"](rel)
    assert c["reader"] == "flow" and c["image"] is None
    assert "Words of a real chapter" in c["text"] and c["tags"] == ["Space Opera"]
    assert "space opera" in svc["tag_vocabulary"]()
    assert svc["content"]("no/such.epub") is None


def test_content_service_comic_gives_first_page(client, upload, prefs, app):
    prefs(target=".epub", mode="none")
    rel = _up(upload, _cbz(), "readme_comic.cbz")
    assert _book(client, rel)
    c = app.module_host.get_service("books")["content"](rel)
    assert c["reader"] == "paged" and c["image"].shape[:2] == (60, 40)


# -- paged books: cache + thumbnails -----------------------------------------------
def test_comic_pages_cache_and_small_thumbs(client, upload, prefs):
    prefs(target=".epub", mode="none")
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for i in range(1, 3):
            im = io.BytesIO()
            Image.new("RGB", (1200, 1800), (i * 60, 90, 160)).save(im, "PNG")
            z.writestr(f"p{i}.png", im.getvalue())
    rel = _up(upload, b.getvalue(), "big_pages.cbz")
    assert _book(client, rel)
    full = client.get(f"/api/books/page/{rel}?n=0")
    assert full.status_code == 200 and "max-age" in full.headers.get("Cache-Control", "")
    thumb = client.get(f"/api/books/page/{rel}?n=0&dpi=72")
    assert thumb.status_code == 200 and thumb.mimetype == "image/jpeg"
    assert max(Image.open(io.BytesIO(thumb.data)).size) <= 320
    ap = br._abs(rel)
    assert bi._page_cache_get(ap, ("page", "p1.png", "full")) == full.data


def test_pdf_page_render_is_cached(tmp_path, monkeypatch):
    fitz = pytest.importorskip("pymupdf")
    p = tmp_path / "c.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "hello")
    doc.save(str(p))
    first = bi.render_pdf_page(str(p), 0, dpi=72)
    assert first and first[:3] == b"\xff\xd8\xff"
    monkeypatch.setattr(bi, "_open_pdf", lambda path: pytest.fail("rendered twice"))
    assert bi.render_pdf_page(str(p), 0, dpi=72) == first


# -- DOCX storage ------------------------------------------------------------------
def test_docx_target_keeps_docx_uploads(prefs):
    prefs(target=".docx", mode="all")
    assert C.target_ext("report.docx") == ".docx"    # stored as it is: no calibre wait
    assert C.target_ext("novel.mobi") == ".docx"
    assert C.target_ext("strip.cbz") == ".cbz"        # comics never become documents


def test_reader_formats_kept_when_converting_unreadable_only(prefs):
    prefs(target=".epub", mode="unsafe")
    assert C.target_ext("report.docx") == ".docx"
    assert C.target_ext("tale.fb2") == ".fb2"
    assert C.target_ext("novel.mobi") == ".epub"