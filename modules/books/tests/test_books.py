"""Books module: an uploaded epub is shelved, readable, and forgotten on delete."""
import time
import pytest


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
