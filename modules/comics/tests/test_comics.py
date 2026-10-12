"""! @file
@brief Comics module: cbz archives on the books shelf (paged reader) and folder comics."""
import copy
import io
import tarfile
import time
import zipfile

import pytest
from cimtest import png_bytes
from modules.encoding import convert


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


# -- comic uploads under Settings -> Media -> Books -----------------------------------------
def _pages(seed):
    return {f"p{i:02}.png": png_bytes(seed=seed + i) for i in range(3)}


def _zip_bytes(pages):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, d in pages.items():
            z.writestr(n, d)
    return b.getvalue()


def _tar_bytes(pages):
    b = io.BytesIO()
    with tarfile.open(fileobj=b, mode="w") as t:
        for n, d in pages.items():
            ti = tarfile.TarInfo(n)
            ti.size = len(d)
            t.addfile(ti, io.BytesIO(d))
    return b.getvalue()


def _7z_bytes(pages):
    b = io.BytesIO()
    with convert.py7zr.SevenZipFile(b, "w") as a:
        for n, d in pages.items():
            a.writestr(d, n)
    return b.getvalue()


@pytest.fixture
def book_mode():
    saved = copy.deepcopy(convert.media_prefs())

    def set_(mode, target=".epub"):
        p = copy.deepcopy(saved)
        p["book"] = {"mode": mode, "target": target}
        convert.set_media_prefs(p)
    yield set_
    convert.set_media_prefs(saved)


@pytest.mark.parametrize("mode,want", [("all", ".cbz"), ("unsafe", None), ("none", None)])
def test_comics_are_never_sent_to_calibre(client, upload, book_mode, mode, want):
    """! @brief With an .epub target a comic stays a comic: repacked to .cbz when everything
    is converted, else kept as uploaded. A .cbr that is really a zip, a .cbt and a .cb7
    (py7zr 1.x has no read()) all make it, with or without a ComicInfo.xml."""
    book_mode(mode)
    made = [("a.cbr", _zip_bytes(_pages(910))), ("b.cbt", _tar_bytes(_pages(920)))]
    if convert.py7zr is not None:
        made.append(("c.cb7", _7z_bytes(_pages(930))))
    for name, data in made:
        j = upload._post({"file": (io.BytesIO(data), name), "mode": "sync", "folder": f"cm_{mode}"}, name)
        fn = j["filename"]
        assert fn.endswith(want or name[-4:]), (name, fn)
        b = _shelf(client, fn)
        assert b and b["kind"] == "comic" and b["page_count"] == 3, (name, b)
        r = client.get(f"/api/books/page/{fn}", query_string={"n": 0})
        assert r.status_code == 200 and r.content_type.startswith("image/")