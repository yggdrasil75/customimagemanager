"""Comics module: cbz archives on the books shelf (paged reader) and folder comics."""
import io
import time
import pytest
from cimtest import png_bytes


def _shelf(client, rel, tries=20):
    for _ in range(tries):
        for b in client.get("/api/books/list").get_json().get("books", []):
            if b["rel_path"] == rel:
                return b
        time.sleep(0.25)
    return None


def test_cbz_shelved_as_paged_comic(client, upload):
    fn = upload.media("comic.cbz")
    b = _shelf(client, fn)
    assert b, "cbz not shelved"
    assert b["kind"] == "comic" and b["reader"] == "paged"
    assert b["page_count"] >= 3


def test_cbz_pages_render(client, upload):
    fn = upload.media("comic.cbz")
    b = _shelf(client, fn)
    for n in range(min(3, b["page_count"])):
        r = client.get(f"/api/books/page/{fn}", query_string={"n": n})
        assert r.status_code == 200 and r.content_type.startswith("image/"), f"page {n}"
    assert client.get(f"/api/books/page/{fn}", query_string={"n": 999}).status_code >= 400


def test_folder_comic_create_get_delete(client, upload):
    folder = "cim_test_comic"
    for i in range(3):
        upload(f"p{i}.png", seed=900 + i, folder=folder)
    j = client.post("/api/comic_create", json={"folder": folder, "title": "Test Comic",
                                               "author": "Me"}).get_json()
    assert j["success"], j
    c = client.get("/api/comic", query_string={"folder": folder}).get_json()
    assert c["success"] and c["comic"]["title"] == "Test Comic" and c["comic"]["author"] == "Me"
    assert client.post("/api/comic_update", json={"folder": folder, "title": "Renamed"}).get_json()["success"]
    assert client.get("/api/comic", query_string={"folder": folder}).get_json()["comic"]["title"] == "Renamed"
    assert client.post("/api/comic_delete", json={"folder": folder}).get_json()["success"]
    assert client.get("/api/comic", query_string={"folder": folder}).get_json()["success"] is False


def test_create_rejects_bad_folder(client):
    assert client.post("/api/comic_create", json={"folder": ""}).get_json()["success"] is False
    assert client.post("/api/comic_create", json={"folder": "../x"}).get_json()["success"] is False
