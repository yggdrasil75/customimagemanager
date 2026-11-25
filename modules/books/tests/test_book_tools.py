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


# -- the Books tab search and the gallery ------------------------------------------
def test_shelf_search_finds_comics_by_file_name(client, upload, prefs):
    """A comic whose title doesn't say "Hulk" (ComicInfo issue title, say) is still
    found by its file name, and every word may match a different field."""
    prefs(target=".epub", mode="none")
    rel = _up(upload, _cbz(), "Incredible Hulk 001 (2023).cbz")
    assert _book(client, rel)
    client.post("/api/books/meta", json={"rel_path": rel, "title": "Gamma Flight"})
    hits = lambda q: [b["rel_path"] for b in client.get(f"/api/books/list?q={q}").get_json()["books"]]
    assert rel in hits("hulk")
    assert rel in hits("gamma 2023")
    assert rel not in hits("hulk 1999")


def test_passage_search_still_lists_title_matches(client, upload, prefs):
    prefs(target=".epub", mode="none")
    rel = _up(upload, _cbz(), "Hulk Smash 002.cbz")
    assert _book(client, rel)
    j = client.post("/api/books/search", json={"q": "hulk"}).get_json()
    assert j["success"], j
    assert rel in [b["rel_path"] for b in j["matches"]]


def _keep_uploads():
    """Books and images stored as uploaded (this box may have no cjxl); the prefs
    fixture puts the settings back afterwards."""
    C.set_media_prefs(dict(C.media_prefs(), book={"target": ".epub", "mode": "none"},
                           image={"target": ".png", "mode": "none"}))


def _gallery(client, q="", folder="mix"):
    """The whole listing, page by page (page_size is the server's)."""
    out, page = [], 0
    while True:
        j = client.get(f"/api/list?q={q}&folder={folder}&page={page}").get_json()
        out += [f.get("filename") or f.get("folder") for f in j["files"]]
        page += 1
        if page * j["page_size"] >= j["total"]:
            return out


def test_books_sort_in_among_the_photos(client, upload, prefs, app, monkeypatch):
    """A book is placed by the gallery's order, not stacked ahead of every photo, and
    paging through gives each tile once, in order."""
    monkeypatch.setitem(app.state, "page_size", 2)
    prefs(target=".epub", mode="none")
    _keep_uploads()
    book = _epub([f'<html><body><h1>One</h1>{_PARA}</body></html>'])
    names = {}
    for i, n in enumerate(("a_photo.png", "c_photo.png", "e_photo.png", "g_photo.png")):
        names[n] = upload(n, seed=100 + i, folder="mix")
    for n in ("b_book.epub", "f_book.epub"):
        j = upload._post({"file": (io.BytesIO(book), n), "mode": "sync", "folder": "mix"}, n)
        names[n] = j["filename"]
        assert _book(client, names[n])
    want = [names[n] for n in sorted(names)]
    assert _gallery(client) == want
    assert _gallery(client, "sort:-path") == want[::-1]


def test_books_without_a_sort_value_sort_like_files_without_one(client, upload, prefs, app, monkeypatch):
    """sort:-tags puts tagged files first; an untagged book sorts with the untagged
    files (by path among them)."""
    monkeypatch.setitem(app.state, "page_size", 3)
    prefs(target=".epub", mode="none")
    _keep_uploads()
    tagged = upload("z_tagged.png", seed=201, folder="mix2")
    assert client.post("/api/bulk_tag", json={"filenames": [tagged], "tags": ["x", "y"]}).get_json()["success"]
    plain = upload("a_plain.png", seed=202, folder="mix2")
    j = upload._post({"file": (io.BytesIO(_epub([f'<html><body>{_PARA}</body></html>'])), "m_book.epub"),
                      "mode": "sync", "folder": "mix2"}, "m_book.epub")
    assert _book(client, j["filename"])
    got = _gallery(client, "sort:-tags", folder="mix2")
    assert got[0] == tagged and got[1:] == [plain, j["filename"]], got


# -- generated covers ---------------------------------------------------------------
def test_a_long_title_stays_above_the_band():
    """The title used to run 6 lines at up to 96px, straight through the band."""
    from modules.books import book_cover as bc
    from PIL import Image
    title = "010 - Fall of the Hulks - The Savage She-Hulks 001 2010 " * 3
    im = Image.open(io.BytesIO(bc.generate(title, ["Someone"], "A Series", 3))).convert("L")
    band_y = int(bc.SIZE[1] * bc.BAND_Y)
    # the strip right above the band is background only: no glyph pixels in it
    strip = im.crop((bc.MARGIN, band_y - 20, bc.SIZE[0] - bc.MARGIN, band_y - 2))
    assert max(strip.getdata()) - min(strip.getdata()) < 40


def _epub_with(names: dict, cover=None):
    """An EPUB holding `names` (under OEBPS/); `cover` names the one its OPF declares."""
    b = io.BytesIO()
    meta = f'<meta name="cover" content="cov"/>' if cover else ""
    item = f'<item id="cov" href="{cover}" media-type="image/png"/>' if cover else ""
    opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
           f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title>{meta}</metadata>'
           f'<manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>{item}</manifest>'
           '<spine><itemref idref="c1"/></spine></package>')
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
                   'version="1.0"><rootfiles><rootfile full-path="OEBPS/content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/c1.xhtml", f"<html><body>{_PARA}</body></html>")
        for n, data in names.items():
            z.writestr("OEBPS/" + n, data)
    return b.getvalue()


def test_old_generated_covers_are_redrawn_and_real_ones_left_alone(tmp_path):
    from modules.books import book_meta_write as bmw
    ours = tmp_path / "ours.epub"
    ours.write_bytes(_epub_with({bmw.COVER_NAME: b"old drawing"}))
    real = tmp_path / "real.epub"
    real.write_bytes(_epub_with({"cover.jpg": b"the publisher's cover"}))
    assert bmw.replace_generated_cover(str(ours), "epub", b"new drawing")
    assert not bmw.replace_generated_cover(str(real), "epub", b"new drawing")
    with zipfile.ZipFile(ours) as z:
        assert z.read("OEBPS/" + bmw.COVER_NAME) == b"new drawing"
    with zipfile.ZipFile(real) as z:
        assert z.read("OEBPS/cover.jpg") == b"the publisher's cover"
        assert "OEBPS/" + bmw.COVER_NAME not in z.namelist()


def test_redraw_pass_records_whose_cover_it_is(client, upload, prefs, app):
    from modules.books import book_cover as bc, book_meta_write as bmw
    prefs(target=".epub", mode="none")
    rel_ours = _up(upload, _epub_with({bmw.COVER_NAME: _png(5)}), "redraw_ours.epub")
    rel_real = _up(upload, _epub_with({"cover.png": _png(6)}, cover="cover.png"), "redraw_real.epub")
    assert _book(client, rel_ours) and _book(client, rel_real)
    db = app.module_host.db()
    for rp in (rel_ours, rel_real):          # as if shelved before covers were recorded
        db.execute("DELETE FROM book_cover_style WHERE rel_path=?", (rp,))
        db.execute("UPDATE books SET cover='x.jpg' WHERE rel_path=?", (rp,))
    db.commit()
    while br.redraw_generated_covers(50):
        pass
    rows = {r["rel_path"]: (r["style"], r["ours"]) for r in
            db.execute("SELECT * FROM book_cover_style WHERE rel_path IN (?, ?)", (rel_ours, rel_real))}
    assert rows[rel_ours] == (bc.STYLE, 1)
    assert rows[rel_real] == (bc.STYLE, 0)
    with zipfile.ZipFile(br._abs(rel_ours)) as z:
        drawn = z.read(next(n for n in z.namelist() if n.endswith(bmw.COVER_NAME)))
    assert drawn[:3] == b"\xff\xd8\xff"       # the new JPEG drawing replaced the old one
    assert br.redraw_generated_covers(50) == 0


def test_a_pdf_without_text_is_still_a_book(client, upload, prefs):
    """PDFs open in the book reader, never the comic reader: no guessing from how
    much text the first pages carry (a scanned novel is a book)."""
    fitz = pytest.importorskip("pymupdf")
    prefs(target=".epub", mode="none")
    doc = fitz.open()
    for _ in range(3):
        doc.new_page(width=1400, height=900).draw_rect(fitz.Rect(10, 10, 1390, 890), color=(1, 0, 0))
    rel = _up(upload, doc.tobytes(), "pictures_only.pdf")    # a book at upload, not an image
    assert _book(client, rel)
    b = client.get(f"/api/books/detail?rel_path={rel}").get_json()["book"]
    assert b["kind"] == "book" and b["reader"] == "paged"
    assert not [f for f in client.get("/api/list").get_json()["files"]
                if f.get("filename") == rel and f.get("kind") == "image"]
