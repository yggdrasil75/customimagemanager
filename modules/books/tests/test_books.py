"""! @file
@brief Books module: an uploaded epub is shelved, readable, and forgotten on delete."""
import io
import os
import shutil
import time
import zipfile
import xml.etree.ElementTree as ET

import pytest
from cimtest import fixture, media_path
from modules.books import book_index as bi, book_routes as br


def _book(client, rel, tries=20):
    for _ in range(tries):
        for b in client.get("/api/books/list").get_json().get("books", []):
            if b["rel_path"] == rel:
                return b
        time.sleep(0.25)
    return None


@pytest.fixture
def epub(upload):
    j = upload.media("book.epub", raw=True)
    assert j.get("media_kind") == "book", j
    return j["filename"]


def test_epub_is_shelved_with_metadata(client, epub):
    b = _book(client, epub)
    assert b, "epub not on the books shelf"
    assert b["kind"] == "book" and b["fmt"] == "epub"
    assert b["title"] and b["title"] != "book", "title should come from the OPF, not the filename"
    assert b["authors"], "authors should come from the OPF"
    authors = {a["name"] for a in client.get("/api/books/authors").get_json()["authors"]}
    assert set(b["authors"]) <= authors


def test_epub_shows_in_gallery_as_book(client, epub):
    row = next((f for f in client.get("/api/list").get_json()["files"] if f.get("filename") == epub), None)
    assert row and row["kind"] == "book"


def test_cover_and_toc(client, epub):
    b = _book(client, epub)
    if b.get("has_cover"):
        try:
            r = client.get(f"/api/books/cover/{epub}")
        except FileNotFoundError as e:
            pytest.fail(f"the shelf says this book has a cover, but /api/books/cover "
                        f"serves a cache file that was never written: {e}")
        assert r.status_code == 200 and r.content_type.startswith("image/")
    r = client.get(f"/api/books/toc/{epub}")
    assert r.status_code == 200 and r.get_json().get("success") is not False


def test_reading_progress_roundtrip(client, epub):
    assert client.post("/api/books/progress", json={"rel_path": epub, "locator": "c1#p3",
                                                    "percent": 42.5}).get_json()["success"]
    p = client.get("/api/books/progress", query_string={"rel_path": epub}).get_json()["progress"]
    assert p["locator"] == "c1#p3" and abs(p["percent"] - 42.5) < 1e-6


def test_delete_removes_from_shelf(client, epub):
    assert _book(client, epub)
    client.post("/api/delete", json={"filename": epub})
    assert _book(client, epub, tries=1) is None


def test_status(client):
    j = client.get("/api/books/status").get_json()
    assert j.get("success", True) and "books" in j


def test_meta_edit_is_written_into_the_epub(client, epub):
    """! @brief An edit lands in the EPUB's OPF, not only in the books row."""
    assert _book(client, epub)
    edit = {"rel_path": epub, "title": "Edited Title", "authors": ["New Author", "Second Author"],
            "series": "The Series", "series_index": 3, "publisher": "Some Press",
            "isbn": "9780306406157", "rating": 4}
    assert client.post("/api/books/meta", json=edit).get_json()["success"]
    fp = media_path(epub)
    m = bi.read_metadata(fp, "epub")
    assert m["title"] == "Edited Title"
    assert m["authors"] == ["New Author", "Second Author"]
    assert m["series"] == "The Series" and m["series_index"] == 3.0
    assert m["publisher"] == "Some Press" and m["isbn"] == "9780306406157" and m["rating"] == 4
    with zipfile.ZipFile(fp) as z:
        assert z.namelist()[0] == "mimetype"
        assert z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
    b = _book(client, epub)
    assert b["title"] == "Edited Title" and b["authors"] == ["New Author", "Second Author"]
    authors = {a["name"] for a in client.get("/api/books/authors").get_json()["authors"]}
    assert {"New Author", "Second Author"} <= authors


def test_meta_edit_without_embedded_home_goes_to_sidecar(client):
    """! @brief A format with no metadata slots (plain text) keeps its edits in
    the XMP sidecar, and reading the book back returns them."""
    p = media_path("tale.txt")
    with open(p, "w") as f:
        f.write("Title: A Tale\nAuthor: Some One\n\n" + "It was a dark night and the rain fell. " * 400)
    br.index_one("tale.txt")
    edit = {"rel_path": "tale.txt", "title": "Sidecar Title", "authors": ["X Y", "Z W"],
            "series": "S", "series_index": 2, "isbn": "9780306406157"}
    assert client.post("/api/books/meta", json=edit).get_json()["success"]
    assert os.path.exists(os.path.splitext(p)[0] + ".xmp")
    m = bi.read_metadata(p, "text")
    assert (m["title"], m["authors"], m["series"], m["series_index"], m["isbn"]) == \
           ("Sidecar Title", ["X Y", "Z W"], "S", 2.0, "9780306406157")


def test_meta_edit_is_written_into_comicinfo(client):
    """! @brief A comic's edit lands in its ComicInfo.xml (through the comics
    module's comicinfo service). The archive is placed and indexed directly so
    the test doesn't depend on the upload path."""
    rel = "metaedit_comic.cbz"
    shutil.copy(fixture("comic.cbz"), media_path(rel))
    br.index_one(rel)
    assert _book(client, rel), "cbz not on the shelf"
    edit = {"rel_path": rel, "title": "Issue One", "authors": ["A Writer"], "series": "Run",
            "series_index": 1, "publisher": "Pub", "published": "2021-03-04"}
    assert client.post("/api/books/meta", json=edit).get_json()["success"]
    with zipfile.ZipFile(media_path(rel)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith("comicinfo.xml"))
        root = ET.fromstring(z.read(name))
    got = {t: (root.findtext(t) or "") for t in ("Title", "Writer", "Series", "Number",
                                                 "Publisher", "Year", "Month", "Day")}
    assert got == {"Title": "Issue One", "Writer": "A Writer", "Series": "Run", "Number": "1",
                   "Publisher": "Pub", "Year": "2021", "Month": "3", "Day": "4"}, got
    b = _book(client, rel)
    assert b["title"] == "Issue One" and b["authors"] == ["A Writer"]


# -- calibre libraries: metadata.opf beside the book -----------------------------------------
CALIBRE_OPF = b"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>The Real Title</dc:title>
    <dc:creator opf:role="aut">Ann Author</dc:creator>
    <dc:publisher>Calibre Press</dc:publisher>
    <meta name="calibre:series" content="The Saga"/>
    <meta name="calibre:series_index" content="3"/>
  </metadata>
</package>"""


def _bare_epub(title):
    """! @brief A minimal epub that names its title and nothing else."""
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("content.opf",
                   '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">'
                   f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{title}</dc:title></metadata>'
                   '<manifest><item id="c1" href="c1.html" media-type="application/xhtml+xml"/></manifest>'
                   '<spine><itemref idref="c1"/></spine></package>')
        z.writestr("c1.html", "<html><body><p>" + "Words of the story. " * 200 + "</p></body></html>")
    return b.getvalue()


def test_calibre_metadata_opf_fills_the_books_gaps(client, upload):
    """! @brief A calibre book folder (book + metadata.opf) uploads whole: the .opf is kept
    beside the book, not rejected, and fills what the book file doesn't say (authors,
    series, publisher) whichever of the two arrives first. The file's own title wins."""
    folder = "calibre_test/Ann Author/The Real Title (12)"
    j = upload._post({"file": (io.BytesIO(_bare_epub("Own Title")), "The Real Title.epub"),
                      "mode": "sync", "folder": folder}, "x.epub")
    rel = j["filename"]
    j = upload._post({"file": (io.BytesIO(CALIBRE_OPF), "metadata.opf"), "mode": "sync",
                      "folder": folder}, "metadata.opf")
    assert j["success"] and j["filename"].endswith("metadata.opf"), j
    assert os.path.exists(media_path(j["filename"]))
    b = _book(client, rel)
    assert b and b["title"] == "Own Title" and b["authors"] == ["Ann Author"], b
    m = bi.read_metadata(media_path(rel), "epub")
    assert (m["series"], m["series_index"], m["publisher"]) == ("The Saga", 3.0, "Calibre Press")
    # the .opf itself is no book, and no stray for triage
    assert bi.classify(media_path(j["filename"])).status == "part"
    assert not _book(client, j["filename"], tries=1)