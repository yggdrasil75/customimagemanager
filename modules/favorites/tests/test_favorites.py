"""! @file
@brief Favorites module: set / unset, list, count, the fav: token, the enricher
flag on list rows, the tag mirror and row cleanup on delete."""
from cimtest import read_meta, post_json

FOLDER = "fav_test"


def _set(client, files, favorite=True):
    j = post_json(client, "/api/favorites/set", {"filenames": files, "favorite": favorite})
    assert j["success"], j
    return j


def _names(client, **q):
    q.setdefault("folder", FOLDER)
    j = client.get("/api/list", query_string=q).get_json()
    assert j["success"], j
    return [f["filename"] for f in j["files"] if isinstance(f, dict) and "filename" in f]


def _rows(host, fn):
    return host.db().execute("SELECT username, rel_path, added FROM favorites WHERE rel_path=?",
                             (fn,)).fetchall()


def test_set_unset_and_count(client, host, upload):
    a = upload(seed=1301, name="fa.png", folder=FOLDER)
    b = upload(seed=1302, name="fb.png", folder=FOLDER)
    before = client.get("/api/favorites/count").get_json()["count"]
    j = _set(client, [a, b])
    assert j["count"] == 2 and sorted(j["files"]) == sorted([a, b])
    assert client.get("/api/favorites/count").get_json()["count"] == before + 2
    rows = _rows(host, a)
    assert len(rows) == 1 and rows[0]["username"] == "" and rows[0]["added"]
    _set(client, [a], favorite=False)
    assert _rows(host, a) == []
    assert client.get("/api/favorites/count").get_json()["count"] == before + 1
    # unknown or missing files are skipped, not an error
    j = _set(client, ["no/such/file.png", "../x.png"])
    assert j["success"] and j["count"] == 0


def test_list_scope_and_order(client, upload):
    a = upload(seed=1311, name="la.png", folder=FOLDER)
    b = upload(seed=1312, name="lb.png", folder=FOLDER)
    c = upload(seed=1313, name="lc.png", folder=FOLDER)
    _set(client, [a])
    _set(client, [b])
    j = client.get("/api/favorites/list", query_string={"folder": FOLDER}).get_json()
    assert j["success"] and j["total"] == 2
    assert [f["filename"] for f in j["files"]] == [b, a]              # newest first
    assert j["files"][0]["added"] and "width" in j["files"][0] and j["files"][0]["kind"] == "image"
    assert c not in [f["filename"] for f in j["files"]]
    p = client.get("/api/favorites/list", query_string={"folder": FOLDER, "limit": 1, "offset": 1}).get_json()
    assert p["total"] == 2 and [f["filename"] for f in p["files"]] == [a]
    stem = a.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    q = client.get("/api/favorites/list", query_string={"folder": FOLDER, "q": stem}).get_json()
    assert [f["filename"] for f in q["files"]] == [a]
    assert client.get("/api/favorites/list", query_string={"q": "sem:cat"}).status_code == 400


def test_token_and_enricher_flag(client, upload):
    a = upload(seed=1321, name="ta.png", folder=FOLDER)
    b = upload(seed=1322, name="tb.png", folder=FOLDER)
    _set(client, [a])
    assert _names(client, q="fav:yes") == [a]
    assert _names(client, q="fav:me") == [a]
    assert _names(client, q="fav:any") == [a]
    assert _names(client, q="fav:no") == [b]
    assert set(_names(client, q="sort:-favorited")[:1]) == {a}
    j = client.get("/api/list", query_string={"folder": FOLDER}).get_json()
    flags = {f["filename"]: f.get("favorite") for f in j["files"] if isinstance(f, dict) and "filename" in f}
    assert flags[a] is True and flags[b] is False
    assert read_meta(client, a)["favorite"] is True
    assert read_meta(client, b)["favorite"] is False


def test_tag_mirror(client, host, upload):
    tag = host.config.get("favorites_tag")
    assert tag == "favorite"
    a = upload(seed=1331, name="ma.png", folder=FOLDER)
    _set(client, [a])
    assert tag in read_meta(client, a)["tags"]
    assert a in _names(client, q=f"tag:{tag}")
    _set(client, [a], favorite=False)
    assert tag not in read_meta(client, a)["tags"]


def test_mirror_off(client, host, upload):
    old = host.config.get("favorites_tag")
    host.set_config("favorites_tag", "", save=False)
    try:
        a = upload(seed=1341, name="oa.png", folder=FOLDER)
        _set(client, [a])
        assert "favorite" not in read_meta(client, a)["tags"]
        assert read_meta(client, a)["favorite"] is True
    finally:
        host.set_config("favorites_tag", old, save=False)


def test_delete_drops_row(client, host, upload):
    a = upload(seed=1351, name="da.png", folder=FOLDER)
    _set(client, [a])
    assert _rows(host, a)
    r = client.post("/api/delete", json={"filename": a}).get_json()
    assert r["success"], r
    assert _rows(host, a) == []


def test_startup_check_seeds_from_tag(client, host, upload):
    tag = host.config.get("favorites_tag")
    a = upload(seed=1361, name="sa.png", folder=FOLDER)
    host.update_file(a, add={"tags": [tag]})
    assert _rows(host, a) == []
    check = next(t["check"] for t in host.db_tables if t["module_id"] == "favorites")
    check(host.db())
    rows = _rows(host, a)
    assert len(rows) == 1 and rows[0]["username"] == ""
    assert read_meta(client, a)["favorite"] is True
