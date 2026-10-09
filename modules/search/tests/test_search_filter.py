"""! @file
@brief search_sort: the filter builder's server side - the kind: token and the
metadata value suggestions (/api/search_sort/values)."""
import pytest
from common import table_exists

ROOT = "zz_search_filter_test"
ROWS = [("p.jxl", "image"), ("v.mp4", "video"), ("r.jxl", "image")]


@pytest.fixture
def rows(app):
    db = app._db()
    for n, k in ROWS:
        db.execute("INSERT OR REPLACE INTO files(rel_path,width,height,tags,media_kind) VALUES(?,?,?,?,?)",
                   (f"{ROOT}/{n}", 100, 100, "[]", k))
    db.execute("INSERT OR REPLACE INTO raws(uid,path,orig_name,derived_rel) VALUES(?,?,?,?)",
               ("zzsf", ".raws/zzsf.cr2", "r.cr2", f"{ROOT}/r.jxl"))
    have_meta = table_exists(db, "metadata_index")
    if have_meta:
        for n, model in (("p.jxl", "Canon EOS R5"), ("r.jxl", "Canon EOS R5"), ("v.mp4", "Pixel 8")):
            db.execute("INSERT OR REPLACE INTO metadata_index(rel_path,ns,tag,value) VALUES(?,?,?,?)",
                       (f"{ROOT}/{n}", "exif", "Model", model))
    db.commit()
    yield have_meta
    db.execute("DELETE FROM files WHERE rel_path LIKE ?", (ROOT + "/%",))
    db.execute("DELETE FROM raws WHERE uid='zzsf'")
    if have_meta:
        db.execute("DELETE FROM metadata_index WHERE rel_path LIKE ?", (ROOT + "/%",))
    db.commit()


def q(app, search):
    entries, _ = app._query_files(search, 0, 100, folder=ROOT)
    return sorted(e["filename"].rsplit("/", 1)[1] for e in entries if e.get("kind") == "image")


@pytest.mark.parametrize("search,want", [
    ("kind:photo", ["p.jxl", "r.jxl"]),
    ("kind:video", ["v.mp4"]),
    ("kind:videos", ["v.mp4"]),
    ("kind:raw", ["r.jxl"]),
    ("kind:raw|video", ["r.jxl", "v.mp4"]),
    ("-kind:raw", ["p.jxl", "v.mp4"]),
])
def test_kind_token(app, rows, search, want):
    assert q(app, search) == want


def test_values_route_counts_models(client, rows):
    r = client.get("/api/search_sort/values?ns=exif&tag=Model")
    assert r.status_code == 200
    d = r.get_json()
    assert d["success"]
    if not rows:
        assert d["values"] == []
        return
    vals = {v["value"]: v["count"] for v in d["values"]}
    assert vals.get("Canon EOS R5", 0) >= 2 and "Pixel 8" in vals
    d = client.get("/api/search_sort/values?ns=exif&tag=Model&q=pixel").get_json()
    assert [v["value"] for v in d["values"]] == ["Pixel 8"]
    assert client.get("/api/search_sort/values").status_code == 400


def test_builder_tokens_are_in_info(client):
    tokens = " ".join(r.get("token", "") for s in client.get("/api/info").get_json()["sections"]
                      if s["id"] == "search" for r in s["rows"])
    assert "kind:" in tokens and "tags:" in tokens and "name:" in tokens
