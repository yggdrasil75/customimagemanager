"""! @file
@brief Timeline module: date buckets, per-period file lists, scope, sort:taken."""
import time
from datetime import datetime, timezone

import numpy as np
import pytest

FOLDER = "tl_test"
COLS = ("d_actual", "d_original", "d_capture", "d_digitized", "d_modified")


def _set_date(host, fn, iso, col="d_original"):
    db = host.db()
    db.execute("UPDATE files SET " + ", ".join(f"{c}=NULL, {c}_epoch=NULL" for c in COLS)
               + " WHERE rel_path=?", (fn,))
    if iso:
        ep = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp()
        db.execute(f"UPDATE files SET {col}=?, {col}_epoch=? WHERE rel_path=?", (iso[:10], ep, fn))
    db.commit()


@pytest.fixture
def lib(host, upload):
    plan = {
        "a.png": "2021-03-04T10:00:00",
        "b.png": "2021-03-04T12:00:00",
        "c.png": "2021-03-20T09:00:00",
        "d.png": "2021-11-01T08:00:00",
        "e.png": "2023-07-15T18:00:00",
        "f.png": None,                       # undated
    }
    out = {}
    for i, (name, iso) in enumerate(plan.items()):
        fn = upload(seed=950 + i, name=name, folder=FOLDER)
        out[name] = fn
    # a capture-bucket date also counts, after original
    for name, iso in plan.items():
        _set_date(host, out[name], iso)
    _set_date(host, out["e.png"], "2023-07-15T18:00:00", col="d_capture")
    return out


def _buckets(client, **q):
    q.setdefault("folder", FOLDER)
    j = client.get("/api/timeline/buckets", query_string=q).get_json()
    assert j["success"], j
    return j


def _files(client, **q):
    q.setdefault("folder", FOLDER)
    j = client.get("/api/timeline/files", query_string=q).get_json()
    assert j["success"], j
    return j


def test_year_buckets(client, lib):
    j = _buckets(client, level="year")
    assert [(b["key"], b["count"]) for b in j["buckets"]] == [("2023", 1), ("2021", 4), ("undated", 1)]
    assert j["total"] == 6
    asc = _buckets(client, level="year", order="asc")
    assert [b["key"] for b in asc["buckets"]] == ["2021", "2023", "undated"]


def test_month_and_day_buckets_with_scope(client, lib):
    j = _buckets(client, level="month", scope="2021")
    assert [(b["key"], b["count"]) for b in j["buckets"]] == [("2021-11", 1), ("2021-03", 3)]
    d = _buckets(client, level="day", scope="2021-03")
    assert [(b["key"], b["count"]) for b in d["buckets"]] == [("2021-03-20", 1), ("2021-03-04", 2)]


def test_samples(client, lib):
    j = _buckets(client, level="year", samples=2)
    by = {b["key"]: b for b in j["buckets"]}
    assert len(by["2021"]["samples"]) == 2
    assert set(by["2021"]["samples"]) <= {lib["a.png"], lib["b.png"], lib["c.png"], lib["d.png"]}
    assert by["2021"]["samples"][0] == lib["d.png"]          # newest first
    assert by["undated"]["samples"] == [lib["f.png"]]
    one = _buckets(client, level="year", samples=9)
    assert len({b["key"]: b for b in one["buckets"]}["2021"]["samples"]) == 4


def test_files_period_order_and_paging(client, lib):
    j = _files(client, period="2021-03")
    assert [f["filename"] for f in j["files"]] == [lib["c.png"], lib["b.png"], lib["a.png"]]
    assert j["files"][0]["date"] == "2021-03-20"
    p = _files(client, period="2021", limit=2, offset=1)
    assert p["total"] == 4 and [f["filename"] for f in p["files"]] == [lib["c.png"], lib["b.png"]]
    assert [f["filename"] for f in _files(client, period="undated")["files"]] == [lib["f.png"]]
    allf = _files(client)
    assert allf["files"][-1]["filename"] == lib["f.png"]       # undated last
    assert allf["total"] == 6


def test_search_scope_applies(client, lib):
    stem = lib["d.png"].rsplit("/", 1)[-1].rsplit(".", 1)[0]
    j = _buckets(client, level="year", q=stem)
    assert [(b["key"], b["count"]) for b in j["buckets"]] == [("2021", 1)]


def test_sort_taken(client, lib):
    j = client.get("/api/list", query_string={"q": "sort:-taken", "folder": FOLDER}).get_json()
    names = [f["filename"] for f in j["files"] if isinstance(f, dict) and "filename" in f]
    assert names[:5] == [lib["e.png"], lib["d.png"], lib["c.png"], lib["b.png"], lib["a.png"]]


def test_bad_requests(client):
    assert client.get("/api/timeline/buckets", query_string={"level": "week"}).status_code == 400
    assert client.get("/api/timeline/buckets", query_string={"scope": "21"}).status_code == 400
    assert client.get("/api/timeline/files", query_string={"period": "2021-3"}).status_code == 400
    assert client.get("/api/timeline/files", query_string={"q": "sem:cat"}).status_code == 400

def test_about_filter_is_accepted_and_scopes(client, host, app, lib, fake_model, monkeypatch):
    """! @brief The non-ranking semantic filter (about:) composes with the timeline;
    the ranked sem: / ~ is refused with a pointer to it."""
    if host.get_service("embedding") is None:
        pytest.skip("embedding module off")
    r = client.get("/api/timeline/buckets", query_string={"q": "sem:cat", "level": "year"})
    assert r.status_code == 400 and "about:" in r.get_json()["error"]

    def model(img, *a, **k):
        return np.ones(2, np.float32) / np.sqrt(2)
    model.space = "tl-about-space"
    model.embed_text = lambda t: np.asarray([1, 0] if t == "cat" else [0, 1], np.float32)
    fake_model("embed", model)
    monkeypatch.setitem(app.state, "semantic_filter_threshold", 0.8)
    monkeypatch.setitem(app.state, "semantic_filter_top", 0)
    db, now = host.db(), time.time()
    vecs = {"a.png": [1, 0], "d.png": [0.95, 0.31], "e.png": [0, 1]}   # a, d: cats
    try:
        for name in lib:
            v = np.asarray(vecs.get(name, [0.2, 0.98]), np.float32)
            v /= np.linalg.norm(v)
            db.execute("INSERT OR REPLACE INTO image_embeddings(rel_path,dim,vec,model,mtime,updated) "
                       "VALUES (?,?,?,?,?,?)", (lib[name], 2, v.tobytes(), model.space, now, now))
        db.commit()
        j = _buckets(client, level="month", q="about:cat")
        assert [(b["key"], b["count"]) for b in j["buckets"]] == [("2021-11", 1), ("2021-03", 1)]
        f = _files(client, period="2021", q="about:cat")
        assert [x["filename"] for x in f["files"]] == [lib["d.png"], lib["a.png"]]
    finally:
        db.execute("DELETE FROM image_embeddings WHERE model=?", (model.space,))
        db.commit()
