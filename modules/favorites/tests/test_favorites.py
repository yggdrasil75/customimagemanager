"""! @file
@brief Favorites module: set / unset, list, count, the fav: token, the enricher
flag on list rows, the tag mirror, row cleanup on delete, and the admin copy in
the file (written on set, rebuilt by a sync pull, written by a sync push; a
non-admin's favorite stays DB-only). `as_` swaps g.user for a real account."""
import contextlib

import pytest
from flask import g

import features
from cimtest import read_meta, post_json

FOLDER = "fav_test"


@pytest.fixture(scope="module")
def accounts(client):
    """! @brief A real non-admin and a real admin account, removed afterwards."""
    out = {}
    for name, admin in (("fav_user", False), ("fav_admin", True)):
        r = client.post("/api/auth/users/create", json={"username": name, "password": "pw",
                                                        "role": "custom", "is_admin": admin})
        assert r.status_code == 200, r.get_json()
        out[name] = r.get_json()["user"]
    yield out
    for u in out.values():
        client.post("/api/auth/users/delete", json={"id": u["id"]})


@contextlib.contextmanager
def as_(app, acct, admin=False):
    """! @brief Run requests as `acct` (non-admin unless `admin`)."""
    def _swap():
        if getattr(g, "user", None) is not None:
            g.user = {**acct, "is_admin": admin, "source": "local",
                      "features": features.effective_permissions("admin" if admin else "custom", {})}
    app.app.before_request_funcs.setdefault(None, []).append(_swap)
    try:
        yield
    finally:
        app.app.before_request_funcs[None].remove(_swap)


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


def _data(host, fn):
    return host.core.file_data(fn, "favorites")


def _users(host, fn):
    return sorted(r["username"] for r in _rows(host, fn))


def _drop_rows(host, fn):
    host.db().execute("DELETE FROM favorites WHERE rel_path=?", (fn,))
    host.db().commit()


def test_admin_favorite_in_file_survives_db_loss(client, host, upload):
    a = upload(seed=1371, name="fa_admin.png", folder=FOLDER)
    _set(client, [a])
    assert _data(host, a) == [""]                 # the anonymous admin
    _drop_rows(host, a)
    assert _rows(host, a) == []
    host.emit("library.sync", direction="pull", rel_paths=[a])
    assert _users(host, a) == [""]
    assert read_meta(client, a)["favorite"] is True
    _set(client, [a], favorite=False)
    assert _data(host, a) is None and _rows(host, a) == []


def test_real_admin_listed_by_name(app, client, host, upload, accounts):
    adm = accounts["fav_admin"]
    a = upload(seed=1372, name="fa_admin2.png", folder=FOLDER)
    with as_(app, adm, admin=True):
        _set(client, [a])
    _set(client, [a])
    assert _data(host, a) == ["", "fav_admin"]
    _drop_rows(host, a)
    host.emit("library.sync", direction="pull", rel_paths=[a])
    assert _users(host, a) == ["", "fav_admin"]
    with as_(app, adm, admin=True):
        _set(client, [a], favorite=False)
    assert _data(host, a) == [""]
    assert "favorite" in read_meta(client, a)["tags"]     # another admin still favors it


def test_non_admin_favorite_stays_in_db(app, client, host, upload, accounts):
    u = accounts["fav_user"]
    a = upload(seed=1373, name="fa_user.png", folder=FOLDER)
    with as_(app, u):
        _set(client, [a])
    assert _users(host, a) == ["fav_user"]
    assert _data(host, a) is None
    assert "favorite" not in read_meta(client, a)["tags"]
    # a pull never touches non-admin rows, a push never writes them
    host.emit("library.sync", direction="pull", rel_paths=[a])
    host.emit("library.sync", direction="push", rel_paths=None)
    assert _users(host, a) == ["fav_user"] and _data(host, a) is None
    with as_(app, u):
        _set(client, [a], favorite=False)
    assert _rows(host, a) == [] and _data(host, a) is None


def test_push_writes_missing_and_pull_drops_unlisted(client, host, upload):
    a = upload(seed=1374, name="fa_push.png", folder=FOLDER)
    b = upload(seed=1375, name="fa_pull.png", folder=FOLDER)
    host.update_file(a, table="favorites", key={"username": ""}, set={"added": 1.0}, dont_write=True)
    assert _data(host, a) is None
    host.emit("library.sync", direction="push", rel_paths=None)
    assert _data(host, a) == [""]
    # an admin row the file does not list goes on a pull; a non-admin row stays
    host.update_file(b, table="favorites", key={"username": ""}, set={"added": 1.0}, dont_write=True)
    host.update_file(b, table="favorites", key={"username": "fav_user"}, set={"added": 1.0},
                     dont_write=True)
    host.emit("library.sync", direction="pull", rel_paths=[b])
    assert _users(host, b) == ["fav_user"]


def test_full_pull_and_startup_seed_from_file_data(client, host, upload):
    a = upload(seed=1376, name="fa_full.png", folder=FOLDER)
    host.core.set_file_data(a, "favorites", [""])
    host.emit("library.sync", direction="pull", rel_paths=None)
    assert _users(host, a) == [""]
    saved = host.db().execute("SELECT * FROM favorites").fetchall()
    host.db().execute("DELETE FROM favorites")
    host.db().commit()
    try:
        check = next(t["check"] for t in host.db_tables if t["module_id"] == "favorites")
        check(host.db())
        assert _users(host, a) == [""]
    finally:
        for r in saved:
            host.db().execute("INSERT OR IGNORE INTO favorites(username, rel_path, added) VALUES (?,?,?)",
                              (r["username"], r["rel_path"], r["added"]))
        host.db().commit()