"""! @file
@brief Book and comic uploads end to end: stored format per Settings > Media, shelved
with the right reader, and readable (pages for comics, text for books)."""
import io
import re
import time
import zipfile

import pytest
from PIL import Image

from modules.encoding import convert as C


def _png(seed):
    b = io.BytesIO()
    Image.new("RGB", (40, 60), (seed * 40 % 256, 90, 160)).save(b, "PNG")
    return b.getvalue()


def _cbz():
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for i in range(1, 5):
            z.writestr(f"page{i:02d}.png", _png(i))
    return b.getvalue()


def _cb7():
    py7zr = pytest.importorskip("py7zr")
    b = io.BytesIO()
    with py7zr.SevenZipFile(b, "w") as a:
        for i in range(1, 5):
            a.writestr(_png(i), f"page{i:02d}.png")
    return b.getvalue()


def _epub(pages_html, images=None):
    """! @brief A minimal epub: spine of xhtml files, optional images beside them."""
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:'
                   'opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf"'
                   ' media-type="application/oebps-package+xml"/></rootfiles></container>')
        items, refs = [], []
        for i, html in enumerate(pages_html):
            n = f"page_{i}.xhtml"
            z.writestr("OEBPS/" + n, html)
            items.append(f'<item id="p{i}" href="{n}" media-type="application/xhtml+xml"/>')
            refs.append(f'<itemref idref="p{i}"/>')
        for i, (name, data) in enumerate((images or {}).items()):
            z.writestr("OEBPS/" + name, data)
            items.append(f'<item id="i{i}" href="{name}" media-type="image/png"/>')
        z.writestr("OEBPS/content.opf",
                   '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
                   '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title>'
                   '</metadata><manifest>' + "".join(items) + '</manifest><spine>'
                   + "".join(refs) + '</spine></package>')
    return b.getvalue()


def _text_epub():
    para = "<p>" + "The quick brown fox jumps over the lazy dog. " * 20 + "</p>"
    return _epub([f'<html><body><h1>Chapter {i}</h1>{para}</body></html>' for i in range(1, 4)])


def _image_epub():
    """! @brief An epub whose pages are only images (a comic converted by calibre)."""
    imgs = {f"images/img{i}.png": _png(i) for i in range(1, 5)}
    return _epub([f'<html><head><title>Page {i}</title></head><body><div><img src="images/img{i}.png"'
                  f' alt="comic page"/></div></body></html>' for i in range(1, 5)], imgs)


def _book(client, rel, tries=40):
    for _ in range(tries):
        for b in client.get("/api/books/list").get_json().get("books", []):
            if b["rel_path"] == rel:
                return b
        time.sleep(0.25)
    return None


@pytest.fixture
def prefs():
    saved = C.media_prefs()
    yield lambda **book: C.set_media_prefs(dict(saved, book=book))
    C.set_media_prefs(saved)


def _up(upload, data, name):
    j = upload._post({"file": (io.BytesIO(data), name), "mode": "sync", "folder": ""}, name)
    assert j.get("media_kind") == "book", j
    return j["filename"]


def _assert_paged(client, rel, pages=4):
    b = _book(client, rel)
    assert b, f"{rel} not shelved"
    assert b["reader"] == "paged" and b["kind"] == "comic", b
    for n in range(pages):
        r = client.get(f"/api/books/page/{rel}?n={n}")
        assert r.status_code == 200, r.get_data(as_text=True)
        assert r.mimetype.startswith("image/")
        Image.open(io.BytesIO(r.data)).verify()


@pytest.mark.parametrize("target,mode", [(".epub", "all"), (".epub", "unsafe"), (".epub", "none"),
                                         (".pdf", "all"), (".cbz", "all"), (".cbz", "none")])
def test_cbz_upload_stays_a_comic(client, upload, prefs, target, mode):
    prefs(target=target, mode=mode)
    rel = _up(upload, _cbz(), f"strip_{target[1:]}_{mode}.cbz")
    assert rel.endswith(".cbz"), rel
    _assert_paged(client, rel)


@pytest.mark.parametrize("mode", ["all", "none"])
def test_cb7_upload_is_readable(client, upload, prefs, mode):
    prefs(target=".epub", mode=mode)
    rel = _up(upload, _cb7(), f"seven_{mode}.cb7")
    assert rel.endswith(".cb7"), rel  # an .epub target leaves comics as they are
    _assert_paged(client, rel)


def test_cb7_repacks_to_cbz_target(client, upload, prefs):
    prefs(target=".cbz", mode="all")
    rel = _up(upload, _cb7(), "seven_to_cbz.cb7")
    assert rel.endswith(".cbz"), rel
    _assert_paged(client, rel)


def test_cbr_that_is_really_a_zip(client, upload, prefs):
    prefs(target=".epub", mode="none")
    rel = _up(upload, _cbz(), "renamed.cbr")
    _assert_paged(client, rel)


@pytest.mark.parametrize("mode", ["all", "none"])
def test_text_epub_upload_reads(client, upload, prefs, mode):
    prefs(target=".epub", mode=mode)
    rel = _up(upload, _text_epub(), f"novel_{mode}.epub")
    assert rel.endswith(".epub")
    b = _book(client, rel)
    assert b and b["reader"] == "flow", b
    r = client.get(f"/api/books/section/{rel}?idx=0").get_json()
    assert r["success"] and "quick brown fox" in r["html"], r


def test_image_only_epub_reads_as_pages(client, upload, prefs):
    """A comic packed as an epub (calibre's comic output) shows its pictures, not a list
    of page_N.xhtml titles."""
    prefs(target=".epub", mode="none")
    rel = _up(upload, _image_epub(), "converted_comic.epub")
    _assert_paged(client, rel)
    b = _book(client, rel)
    assert b["page_count"] == 4 and b["has_cover"], b


def test_svg_wrapped_epub_comic_reads_as_pages(client, upload, prefs):
    prefs(target=".epub", mode="none")
    imgs = {f"img{i}.png": _png(i) for i in range(1, 4)}
    pages = [f'<html><body><svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
             f'<image xlink:href="img{i}.png"/></svg></body></html>' for i in range(1, 4)]
    rel = _up(upload, _epub(pages, imgs), "svg_comic.epub")
    _assert_paged(client, rel, pages=3)


def test_illustrated_epub_serves_its_pictures(client, upload, prefs):
    """A text book with a picture: the section's <img> points at a route that answers."""
    prefs(target=".epub", mode="none")
    para = "<p>" + "Words of a real chapter go here. " * 20 + "</p>"
    rel = _up(upload, _epub([f'<html><body><h1>One</h1>{para}<img src="fig.png"/></body></html>',
                             f'<html><body><h1>Two</h1>{para}</body></html>'],
                            {"fig.png": _png(7)}), "illustrated.epub")
    b = _book(client, rel)
    assert b and b["reader"] == "flow", b
    r = client.get(f"/api/books/section/{rel}?idx=0").get_json()
    assert r["success"] and r["title"] == "One", r
    m = re.search(r'<img src="([^"]+)"', r["html"])
    assert m and m.group(1).startswith("/api/books/asset/"), r["html"]
    img = client.get(m.group(1))
    assert img.status_code == 200 and img.mimetype == "image/png"
    assert client.get(f"/api/books/asset/{rel}?name=OEBPS/content.opf").status_code == 400


def test_shelved_image_epub_is_reshelved_as_comic(client, upload, prefs, monkeypatch):
    """An image epub shelved as a text book (before comics were recognised) is shelved
    again as a comic by the next library pass."""
    from modules.books import book_index as bi, book_routes as br
    prefs(target=".epub", mode="none")
    monkeypatch.setattr(bi, "epub_is_comic", lambda p: False)
    rel = _up(upload, _image_epub(), "old_conversion.epub")
    assert _book(client, rel)["reader"] == "flow"
    monkeypatch.undo()
    br.reconcile()
    _assert_paged(client, rel)