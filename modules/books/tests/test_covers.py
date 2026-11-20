"""! @file
@brief Generated covers: a book whose file has no cover gets one from its title /
author, written into the format's own cover slot (EPUB manifest + meta, FB2
coverpage + binary, DOCX package thumbnail) and read back from there; formats with
no slot keep it in the app only; the setting turns it off."""
import io
import os
import zipfile
import xml.etree.ElementTree as ET

from PIL import Image

from cimtest import media_path
from modules.books import book_cover, book_index as bi, book_meta_write as bmw, book_routes as br

OPF = "{http://www.idpf.org/2007/opf}"


def _epub(path, title="No Cover Here", version="2.0"):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:'
                   'opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf"'
                   ' media-type="application/oebps-package+xml"/></rootfiles></container>')
        para = "<p>" + "The quick brown fox jumps over the lazy dog. " * 30 + "</p>"
        z.writestr("OEBPS/c1.xhtml", f"<html><body><h1>One</h1>{para}</body></html>")
        z.writestr("OEBPS/content.opf",
                   f'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="{version}">'
                   '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   f'<dc:title>{title}</dc:title><dc:creator>Ann Author</dc:creator><dc:language>en</dc:language>'
                   '</metadata><manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>'
                   '</manifest><spine><itemref idref="c1"/></spine></package>')
    return str(path)


def _fb2(path):
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" '
        'xmlns:l="http://www.w3.org/1999/xlink"><description><title-info>'
        '<genre>prose</genre><author><first-name>Fb</first-name><last-name>Writer</last-name></author>'
        '<book-title>Fb Book</book-title><lang>en</lang></title-info></description>'
        '<body><section><p>' + "Words and more words. " * 40 + '</p></section></body></FictionBook>',
        encoding="utf-8")
    return str(path)


def _docx(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
                   'content-types"><Default Extension="xml" ContentType="application/xml"/>'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
                   'relationships+xml"/></Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
                   'openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                   'Target="word/document.xml"/></Relationships>')
        z.writestr("docProps/core.xml",
                   '<?xml version="1.0"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.'
                   'org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/'
                   '1.1/"><dc:title>Doc Book</dc:title><dc:creator>Dee Writer</dc:creator>'
                   '</cp:coreProperties>')
        z.writestr("word/document.xml", "<w:document xmlns:w=\"http://schemas.openxmlformats.org/"
                   "wordprocessingml/2006/main\"><w:body/></w:document>")
    return str(path)


def test_generated_cover_is_a_portrait_jpeg():
    data = book_cover.generate("A Title " * 20, ["One", "Two", "Three"], "A Series", 2.5)
    im = Image.open(io.BytesIO(data))
    assert im.format == "JPEG" and im.size == book_cover.SIZE
    # the same book always gets the same colour
    a = Image.open(io.BytesIO(book_cover.generate("Same", ["X"]))).getpixel((5, 5))
    b = Image.open(io.BytesIO(book_cover.generate("Same", ["X"]))).getpixel((5, 5))
    assert a == b


def test_epub_cover_written_into_the_file_and_read_back(tmp_path):
    for version in ("2.0", "3.0"):
        p = _epub(tmp_path / f"b{version}.epub", version=version)
        assert not bi.read_metadata(p, "epub").get("cover_bytes")
        jpg = book_cover.generate("No Cover Here", ["Ann Author"])
        assert bmw.write_cover(p, "epub", jpg) == {"written": True, "error": ""}
        assert bi.read_metadata(p, "epub")["cover_bytes"] == jpg
        with zipfile.ZipFile(p) as z:
            assert z.namelist()[0] == "mimetype"                  # still a valid epub
            root = ET.fromstring(z.read("OEBPS/content.opf"))
        item = next(e for e in root.iter(OPF + "item") if e.get("id") == bmw.COVER_ID)
        assert item.get("href") == bmw.COVER_NAME and item.get("media-type") == "image/jpeg"
        assert (item.get("properties") == "cover-image") == (version == "3.0")
        assert any(e.get("name") == "cover" and e.get("content") == bmw.COVER_ID
                   for e in root.iter(OPF + "meta"))
        # a second run leaves it alone
        assert bmw.write_cover(p, "epub", jpg)["written"] is False


def test_fb2_and_docx_covers(tmp_path):
    jpg = book_cover.generate("Fb Book", ["Fb Writer"])
    p = _fb2(tmp_path / "b.fb2")
    assert bmw.write_cover(p, "fb2", jpg)["written"]
    assert bi.read_metadata(p, "fb2")["cover_bytes"] == jpg
    assert bi.read_metadata(p, "fb2")["title"] == "Fb Book"       # rest of the file intact
    assert "coverpage" in open(p, encoding="utf-8").read()
    assert bmw.write_cover(p, "fb2", jpg)["written"] is False
    d = _docx(tmp_path / "b.docx")
    assert bmw.write_cover(d, "docx", jpg)["written"]
    assert bi.read_metadata(d, "docx")["cover_bytes"] == jpg
    with zipfile.ZipFile(d) as z:
        assert "thumbnail" in z.read("_rels/.rels").decode()
        assert 'Extension="jpeg"' in z.read("[Content_Types].xml").decode()
    assert bmw.write_cover(d, "docx", jpg)["written"] is False


def test_format_without_a_slot_is_not_touched(tmp_path):
    p = tmp_path / "t.txt"
    p.write_text("hello")
    assert bmw.write_cover(str(p), "text", b"x") == {"written": False, "error": ""}
    assert p.read_text() == "hello"


def _row(rel):
    return br._db().execute("SELECT cover, mtime FROM books WHERE rel_path=?", (rel,)).fetchone()


def test_indexing_a_coverless_epub_gives_it_one(client):
    rel = "cover_gen_book.epub"
    p = _epub(media_path(rel), title="Shelf Without Cover")
    try:
        br.index_one(rel)
        r = _row(rel)
        assert r and r["cover"], "no cover on the shelf"
        assert abs(r["mtime"] - os.stat(p).st_mtime) < 1e-6        # no re-index for our own write
        assert bi.read_metadata(p, "epub").get("cover_bytes")       # it is in the file
        assert client.get("/api/books/cover/" + rel).status_code == 200
    finally:
        os.remove(p)
        br.remove_book(rel)


def test_text_book_gets_a_cached_cover_and_the_setting_turns_it_off(client, monkeypatch):
    rel = "cover_gen_tale.txt"
    p = media_path(rel)
    with open(p, "w") as f:
        f.write("Title: A Plain Tale\nAuthor: Some One\n\n" + "It was a dark night. " * 400)
    try:
        br.index_one(rel)
        assert _row(rel)["cover"]                                   # cache only: no slot in a .txt
        assert open(p).read().startswith("Title: A Plain Tale")
        br.remove_book(rel)
        monkeypatch.setitem(br.CTX, "generate_covers", lambda: False)
        br.index_one(rel)
        assert not _row(rel)["cover"]
        assert br.backfill_covers() == 0
        monkeypatch.setitem(br.CTX, "generate_covers", lambda: True)
        assert br.backfill_covers() >= 1 and _row(rel)["cover"]      # books shelved while it was off
    finally:
        os.remove(p)
        br.remove_book(rel)